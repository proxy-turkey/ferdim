// FerDim GNOME Shell eklentisi: org.ferdim.Daemon1 koprusunu izler.
// Panelde toplam hiz + etkin is sayisi; menude isler ve web arayuzu.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'org.ferdim.Daemon';
const OBJECT_PATH = '/org/ferdim/Daemon';
const INTERFACE = 'org.ferdim.Daemon1';
const REFRESH_SECONDS = 2;

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

class FerDimIndicator extends PanelMenu.Button {
    constructor() {
        super(0.0, 'FerDim');

        const box = new St.BoxLayout();
        box.add_child(new St.Icon({
            icon_name: 'folder-download-symbolic',
            style_class: 'system-status-icon',
        }));
        this._label = new St.Label({text: ''});
        box.add_child(this._label);
        this.add_child(box);

        this._proxy = null;
        this._signature = '';
        this._jobs = [];
        this._pollId = 0;

        try {
            this._proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, null,
                BUS_NAME, OBJECT_PATH, INTERFACE, null);
            this._proxy.connect('g-signal', (_proxy, _sender, signal, params) => {
                if (signal !== 'Changed')
                    return;
                const [json] = params.deepUnpack();
                this._apply(json);
            });
        } catch (e) {
            this._proxy = null;
        }

        this._refresh();
        this._pollId = GLib.timeout_add_seconds(REFRESH_SECONDS, () => {
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
        const jobs = Array.isArray(stats.jobs) ? stats.jobs : [];
        const active = jobs.filter(j =>
            j.state === 'connecting' || j.state === 'downloading');
        const speed = Number(stats.total_speed) || 0;

        this._label.text = active.length
            ? `↓ ${formatSpeed(speed)} · ${active.length}`
            : '';

        const signature = JSON.stringify(jobs.map(j =>
            [j.id, j.state, j.filename, j.speed]));
        if (signature !== this._signature) {
            this._signature = signature;
            this._jobs = jobs;
            this._rebuildMenu();
        }
    }

    _rebuildMenu() {
        this.menu.removeAll();
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
                item.connect('activate', () => this._call(method, job.id));
                submenu.menu.addMenuItem(item);
            }
            this.menu.addMenuItem(submenu);
        }

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const web = new PopupMenu.PopupMenuItem('Open web UI');
        web.connect('activate', () => this._openWebUI());
        this.menu.addMenuItem(web);
    }

    _openWebUI() {
        const state = GLib.getenv('XDG_STATE_HOME') ||
            GLib.build_filenamev([GLib.get_home_dir(), '.local', 'state']);
        let port = '8378';
        try {
            const [ok, data] = GLib.file_get_contents(
                GLib.build_filenamev([state, 'ferdim', 'port']));
            if (ok) {
                const text = new TextDecoder('utf-8').decode(data).trim();
                if (text)
                    port = text;
            }
        } catch (e) {
            // varsayilan port
        }
        Gio.AppInfo.launch_default_for_uri(`http://127.0.0.1:${port}/`, null);
    }

    destroy() {
        if (this._pollId) {
            GLib.source_remove(this._pollId);
            this._pollId = 0;
        }
        this._proxy = null;
        super.destroy();
    }
}

export default class FerDimExtension extends Extension {
    enable() {
        this._indicator = new FerDimIndicator();
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
