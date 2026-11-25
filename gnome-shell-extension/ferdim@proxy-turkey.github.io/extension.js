// SPDX-License-Identifier: MIT
// FerDim GNOME Shell eklentisi: org.ferdim.Daemon1 koprusunu izler.
// Panelde toplam hiz + etkin is sayisi; menude isler ve web arayuzu.
//
// EGO inceleme kurali: modul yuklenirken ve Extension yapicisinda hicbir
// nesne olusturulmaz; her sey enable() icinde kurulur, disable() hepsini
// birakir. Bu yuzden butun sinyal/zamanlayici kayitlari tek defterden gecer.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'org.ferdim.Daemon';
const OBJECT_PATH = '/org/ferdim/Daemon';
const INTERFACE = 'org.ferdim.Daemon1';
// Panel `Changed` sinyaliyle beslenir (kopru kucuk kare yollar: toplamlar +
// etkin isler). Bu yoklama yalniz kurtarma icin: kopru/daemon yeniden
// baslarsa ya da bir sinyal kacarsa gorunum en gec bu kadar sonra duzelir.
const RECOVERY_SECONDS = 30;

function formatSpeed(bytes) {
    if (!bytes || bytes < 1)
        return '0 B/s';
    const units = ['B/s', 'KB/s', 'MB/s', 'GB/s', 'TB/s'];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
        value /= 1024;
        unit++;
    }
    const digits = unit === 0 || value >= 100 ? 0 : 1;
    return `${value.toFixed(digits)} ${units[unit]}`;
}

// Sinyal ve ana dongu kaynaklari defteri. Eklenti dogrudan connect() /
// timeout_add() cagirmaz: kayit burada tutulur, release() hepsini birakir.
// Boylece "her connect'in disable() tarafinda bir karsiligi var" kurali
// tek noktadan saglanir ve denetlenebilir kalir.
class Tracker {
    constructor() {
        this._signals = [];
        this._sources = [];
    }

    connect(object, signal, callback) {
        const id = object.connect(signal, callback);
        this._signals.push([object, id]);
    }

    timeout(seconds, callback) {
        const id = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, seconds, callback);
        this._sources.push(id);
    }

    release() {
        for (const [object, id] of this._signals)
            object.disconnect(id);
        this._signals = [];
        for (const id of this._sources)
            GLib.Source.remove(id);
        this._sources = [];
    }
}

// PanelMenu.Button bir GObject alt sinifi: ESM'de de registerClass gerekir,
// yoksa enable() "Tried to construct an object without a GType" ile duser.
const FerDimIndicator = GObject.registerClass(
class FerDimIndicator extends PanelMenu.Button {
    constructor() {
        super(0.0, 'FerDim');

        // enable() icinden cagrilir; modul yuklenirken calismaz.
        this._tracker = new Tracker();
        this._menuTracker = null;
        this._proxy = null;
        this._signature = '';
        this._jobs = [];
        this._menuDirty = false;

        const box = new St.BoxLayout();
        box.add_child(new St.Icon({
            icon_name: 'folder-download-symbolic',
            style_class: 'system-status-icon',
        }));
        this._label = new St.Label({text: ''});
        box.add_child(this._label);
        this.add_child(box);

        this._tracker.connect(this.menu, 'open-state-changed', (_menu, open) => {
            if (!open && this._menuDirty) {
                this._menuDirty = false;
                this._rebuildMenu();
            }
        });

        try {
            this._proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, null,
                BUS_NAME, OBJECT_PATH, INTERFACE, null);
            this._tracker.connect(this._proxy, 'g-signal',
                (_proxy, _sender, signal, params) => {
                    if (signal !== 'Changed')
                        return;
                    const [json] = params.deepUnpack();
                    this._apply(json);
                });
        } catch (e) {
            // kopru kurulu degil: panel bos kalir, menu yalnizca web
            // arayuzunu acar. Tek satir uyari, enable basina bir kez.
            this._proxy = null;
            console.warn(`FerDim: D-Bus bridge unavailable (${e.message})`);
        }

        this._refresh();
        this._tracker.timeout(RECOVERY_SECONDS, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _call(method, ...args) {
        if (!this._proxy)
            return;
        const params = args.length ? new GLib.Variant('(s)', args) : null;
        this._proxy.call(method, params, Gio.DBusCallFlags.NONE, -1, null,
            (proxy, result) => {
                try {
                    proxy.call_finish(result);
                } catch (e) {
                    // kopru kapali: sessizce yoksay
                }
            });
    }

    _refresh() {
        if (!this._proxy)
            return;
        this._proxy.call('Stats', null, Gio.DBusCallFlags.NONE, -1, null,
            (proxy, result) => {
                // disable() sonrasi gec gelen yanit: artikel yok sayilir
                if (this._proxy === null)
                    return;
                try {
                    const [json] = proxy.call_finish(result).deepUnpack();
                    this._apply(json);
                } catch (e) {
                    // daemon kapali olabilir; bir sonraki turda yeniden dene
                }
            });
    }

    _apply(json) {
        let stats;
        try {
            stats = JSON.parse(json);
        } catch (e) {
            return;
        }
        // Kopru `Changed`i kucuk tasir: toplamlar + etkin isler (biten ve
        // iptal edilen isler listede yok). Ayni alanlar `Stats` yanitinda da
        // var; iki yol da burada tek bicime iner.
        const jobs = Array.isArray(stats.jobs) ? stats.jobs : [];
        const active = Number.isInteger(stats.active)
            ? stats.active
            : jobs.filter(j =>
                j.state === 'connecting' || j.state === 'downloading').length;
        const speed = Number(stats.total_speed) || 0;

        this._label.text = active
            ? `↓ ${formatSpeed(speed)} · ${active}`
            : '';

        const signature = JSON.stringify(jobs.map(j =>
            [j.id, j.state, j.filename]));
        if (signature !== this._signature) {
            this._signature = signature;
            this._jobs = jobs;
            // menu acikken yeniden kurmak odagi bozar: kapanista yapilir
            if (this.menu.isOpen)
                this._menuDirty = true;
            else
                this._rebuildMenu();
        }
    }

    _rebuildMenu() {
        // once eski ogelerin sinyalleri birakilir, sonra ogeler yok edilir
        this._menuTracker?.release();
        this._menuTracker = new Tracker();
        this.menu.removeAll();
        const tracker = this._menuTracker;

        const visible = this._jobs.filter(j =>
            j.state !== 'done' && j.state !== 'canceled');

        if (!visible.length) {
            const empty = new PopupMenu.PopupMenuItem('No active downloads');
            empty.setSensitive(false);
            this.menu.addMenuItem(empty);
        }

        for (const job of visible) {
            const submenu = new PopupMenu.PopupSubMenuMenuItem(
                `${job.filename || job.url} · ${job.state}`);
            for (const [label, method] of [
                ['Pause', 'Pause'],
                ['Resume', 'Resume'],
                ['Cancel', 'Cancel'],
            ]) {
                const item = new PopupMenu.PopupMenuItem(label);
                tracker.connect(item, 'activate',
                    () => this._call(method, job.id));
                submenu.menu.addMenuItem(item);
            }
            this.menu.addMenuItem(submenu);
        }

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const web = new PopupMenu.PopupMenuItem('Open web UI');
        tracker.connect(web, 'activate', () => this._openWebUI());
        this.menu.addMenuItem(web);
    }

    _openWebUI() {
        // URL daemon'dan gelir: tek kullanimlik nonce, adres onbelleklenmez.
        if (!this._proxy) {
            logError(new Error('FerDim: D-Bus bridge unavailable'));
            return;
        }
        this._proxy.call('WebUrl', null, Gio.DBusCallFlags.NONE, -1, null,
            (proxy, result) => {
                // disable() sonrasi gec gelen yanit: artikel yok sayilir
                if (this._proxy === null)
                    return;
                try {
                    const [url] = proxy.call_finish(result).deepUnpack();
                    if (url)
                        Gio.AppInfo.launch_default_for_uri(url, null);
                } catch (e) {
                    logError(e);
                }
            });
    }

    // disable() buradan gecer: once sinyaller, sonra kaynaklar, sonra
    // pencere ogeleri birakilir; geriye GObject referansi kalmaz.
    destroy() {
        this._menuTracker?.release();
        this._menuTracker = null;
        this._tracker.release();
        this._proxy = null;
        this._jobs = [];
        this._label = null;
        super.destroy();
    }
});

export default class FerDimExtension extends Extension {
    // Yapici yok: EGO kurali geregi modul/yapici tarafinda is yapilmaz.
    enable() {
        this._indicator = new FerDimIndicator();
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
