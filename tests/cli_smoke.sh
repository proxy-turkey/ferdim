#!/bin/bash
# Gercek daemon uzerinde CLI duman testi. Kullanici verisine dokunmaz:
# daemon'in indirme dizini test suresinde gecici dizine cevrilir, temizlik
# yalniz bu betigin ekledigi islereler (kimlikle) yapilir.
set -u
REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
f=ferdim; S=/tmp/ferdim-smoke; FAIL=0; MINE="$S/mine.txt"
ok()  { echo "  PASS $1"; }
bad() { echo "  FAIL $1 -> ${2:-}"; FAIL=1; }
fld() { $f list --json | python3 -c "import json,sys; j=[x for x in json.load(sys.stdin)['jobs'] if x['id']==sys.argv[1]]; print(j[0].get(sys.argv[2]) if j else 'YOK')" "${1:-?}" "${2:-state}"; }
newid() { python3 -c "import sys,re; m=re.search(r'\b([0-9a-f]{10})\b', sys.stdin.read()); print(m.group(1) if m else '')"; }
add() { local id; id=$($f add "$@" 2>/dev/null | newid); [ -n "$id" ] && echo "$id" >> "$MINE"; sleep 0.5; echo "$id"; }
api() { python3 - "$@" <<'PY'
import json, os, sys, urllib.request
st = os.path.expanduser("~/.local/state/ferdim/")
req = urllib.request.Request(f"http://127.0.0.1:{open(st + 'port').read().strip()}/api/{sys.argv[1]}",
    data=json.dumps(json.loads(sys.argv[2])).encode() if len(sys.argv) > 2 else None,
    headers={"X-FerDim-Token": open(st + "token").read().strip(),
             "Content-Type": "application/json"},
    method="POST" if len(sys.argv) > 2 else "GET")
print(json.dumps(json.loads(urllib.request.urlopen(req, timeout=15).read())))
PY
}
wait_done() { for _ in $(seq 1 120); do case "$(fld $1 state)" in done|error|canceled) break;; esac; sleep 0.25; done; }

rm -rf $S; mkdir -p $S/dl $S/watch; : > "$MINE"
PREV_DL=$(api config | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['config'].get('download_dir')))")
PREV_WD=$(api config | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['config'].get('watch_dirs', [])))")
PREV_TURBO=$(api stats | python3 -c "import json,sys; print(json.load(sys.stdin)['turbo'])")
restore() {
  api config "{\"config\":{\"download_dir\":$PREV_DL,\"watch_dirs\":$PREV_WD}}" > /dev/null
  [ "$PREV_TURBO" = True ] && $f turbo on > /dev/null 2>&1 || $f turbo off > /dev/null 2>&1
  [ -z "${SRV_PID:-}" ] || kill "$SRV_PID" 2>/dev/null
  for j in $(sort -u "$MINE"); do [ -n "$j" ] && $f remove "$j" > /dev/null 2>&1; done
  # gecici dizine inmis her sey bu testindir
  for j in $($f list --json | python3 -c "import json,sys; print(' '.join(x['id'] for x in json.load(sys.stdin)['jobs'] if x['dir']=='$S/dl'))"); do $f remove "$j" > /dev/null 2>&1; done
}
trap restore EXIT
python3 "$REPO/tests/ui/srv.py" $S/www 0 > $S/srv.port 2>/dev/null & SRV_PID=$!
for _ in $(seq 1 40); do [ -s $S/srv.port ] && break; sleep 0.1; done
B="http://127.0.0.1:$(head -1 $S/srv.port)"
r=$(api config "{\"config\":{\"download_dir\":\"$S/dl\"}}")
[ "$(api stats | python3 -c "import json,sys; print(json.load(sys.stdin)['download_dir'])")" = "$S/dl" ] && ok "indirme dizini ayari CANLI degisti (yeniden baslatma yok)" || bad "indirme dizini" "$(api stats | python3 -c "import json,sys; print(json.load(sys.stdin)['download_dir'])")"
echo "dosya sunucusu: $B | $($f --version) | locale: $LANG"

# 1) yan dosya dogrulamali indirme + --wait
id=$(add $B/arsiv.zip --wait); st=$(fld $id state); ck=$(fld $id checksum); vf=$(fld $id verified)
[ "$st" = done ] && [ "$ck" = ok ] && [ "$vf" = "['sha256']" ] && ok "indirme bitti; sha256 dogrulandi; --wait dogrulamadan SONRA dondu" || bad "yan dosya" "state=$st checksum=$ck verified=$vf"
cmp -s $S/dl/arsiv.zip $S/www/arsiv.zip && ok "dosya icerigi birebir ayni" || bad "dosya icerigi"
# 2) birincil kaynak kapali -> ayna
id=$(add http://127.0.0.1:1/yok-iso.iso --mirror $B/arsiv.zip -o aynali.zip --wait)
[ "$(fld $id state)" = done ] && ok "birincil kaynak asagi: ayna isi tamamladi" || bad "ayna" "$(fld $id state) $(fld $id error)"
# 3) bayat yan dosya -> uyari
id=$(add $B/rapor.pdf --wait)
[ "$(fld $id state)" = done ] && [ "$(fld $id checksum)" = warn ] && [ -f $S/dl/rapor.pdf ] && ok "bayat yan dosya: is bitti + uyari, dosya korundu" || bad "bayat yan dosya" "$(fld $id state) $(fld $id checksum)"
# 4) gecersiz saglama reddedilir
$f add $B/sarki.mp3 --checksum zzz > $S/o4.txt 2>&1 && bad "gecersiz --checksum kabul edildi" || ok "gecersiz --checksum reddedildi"
$f add $B/sarki.mp3 --sha256 abc > /dev/null 2>&1 && bad "--sha256 abc kabul edildi" || ok "--sha256 abc reddedildi"
$f add $B/sarki.mp3 --checksum "md5:$(python3 -c "import hashlib;print(hashlib.md5(open('$S/www/sarki.mp3','rb').read()).hexdigest())")" --wait > /dev/null 2>&1; sid=$($f list --json | python3 -c "import json,sys; print(next((j['id'] for j in json.load(sys.stdin)['jobs'] if j['filename']=='sarki.mp3'),''))"); echo "$sid" >> "$MINE"
[ "$(fld $sid state)" = done ] && [ "$(fld $sid verified)" = "['md5']" ] && ok "--checksum algo:ozet ile md5 dogrulandi" || bad "algo:ozet" "$(fld $sid state) $(fld $sid verified)"
# 5) liste dosyasi
printf "# yorum\n$B/yazilim.deb\n$B/sarki.mp3\n\n" > $S/liste.txt
n=$($f add @$S/liste.txt 2>&1 | grep -oE "[0-9]+" | tail -1)
[ "$n" = 2 ] && ok "ferdim add @liste.txt -> 2 is eklendi" || bad "liste" "n=$n"
# liste isleri: kimlik URL'den bulunur (dosya adi bitiste "(1)" alabilir)
lids=$($f list --json | python3 -c "import json,sys; ids=[j['id'] for j in json.load(sys.stdin)['jobs'] if j['dir']=='$S/dl' and j['url'].rsplit('/',1)[-1] in ('yazilim.deb','sarki.mp3') and j['id']!='$sid']; print(' '.join(ids))")
echo "$lids" | tr ' ' '\n' >> "$MINE"
for j in $lids; do wait_done $j; done
n_ok=$($f list --json | python3 -c "import json,sys; print(sum(1 for j in json.load(sys.stdin)['jobs'] if j['id'] in '$lids'.split() and j['state']=='done'))")
[ "$n_ok" = 2 ] && ok "listeden eklenen 2 is de bitti (kimlikler: $lids)" || bad "liste isleri" "n_ok=$n_ok ids=[$lids]"
# 6) KIMLIKLE DENETIM (hiz limitli, 32 isci kovada bekliyor)
id=$($f add $B/slow/yavas.iso --limit 250K | newid); echo "$id" >> "$MINE"; sleep 2.5
t0=$(date +%s.%N); $f pause $id > /dev/null 2>&1; prc=$?; dt=$(python3 -c "import time; print(round(time.time()-$t0,2))")
[ $prc -eq 0 ] && [ "$(fld $id state)" = paused ] && python3 -c "import sys; sys.exit(0 if $dt < 3 else 1)" && ok "ferdim pause $id -> 'paused' (${dt} sn)" || bad "pause" "rc=$prc durum=$(fld $id state) dt=$dt"
$f resume $id > /dev/null 2>&1; sleep 0.4; [ "$(fld $id state)" = downloading ] && ok "ferdim resume $id -> 'downloading'" || bad "resume" "$(fld $id state)"
$f pause $id > /dev/null 2>&1; [ "$(fld $id state)" = paused ] && ok "ikinci pause hizli" || bad "ikinci pause" "$(fld $id state)"
$f cancel $id > /dev/null 2>&1; [ "$(fld $id state)" = canceled ] && ok "ferdim cancel $id -> 'canceled'" || bad "cancel" "$(fld $id state)"
$f remove $id > /dev/null 2>&1; [ "$(fld $id state)" = YOK ] && ok "ferdim remove $id -> kuyruktan silindi" || bad "remove" "$(fld $id state)"
# 7) duraklat -> devam -> tamamlanma (butunluk)
id=$($f add $B/slow/yavas.iso -o yavas2.iso --limit 900K | newid); echo "$id" >> "$MINE"; sleep 1.5
$f pause $id > /dev/null 2>&1; meta=~/.local/state/ferdim/jobs/$id.json
[ "$(fld $id state)" = paused ] && [ -f "$meta" ] && ok "denetim noktasi yazildi" || bad "nokta" "$(fld $id state)"
python3 - "$meta" <<'PY'
import json, sys
m = json.load(open(sys.argv[1])); left = sum(e - s + 1 for s, e in m["ranges"])
print("  PASS   araliklar + indirilen = toplam" if left + m["done"] == m["total"] else f"  FAIL   nokta tutarsiz: {left}+{m['done']} != {m['total']}")
PY
$f resume $id > /dev/null 2>&1; wait_done $id
[ "$(fld $id state)" = done ] && [ "$(stat -c %s "$S/dl/yavas2.iso")" = 6291456 ] && ok "duraklat-devam sonrasi 6.0 MB hatasiz tamamlandi" || bad "duraklat-devam" "$(fld $id state) $(fld $id error)"
# 8) bastan indir
$f restart $id > /dev/null 2>&1; sleep 0.4; st1=$(fld $id state)
case "$st1" in queued|connecting|downloading) ok "ferdim restart $id -> yeniden basladi ($st1)";; *) bad "restart" "$st1";; esac
wait_done $id; [ "$(fld $id state)" = done ] && ok "bastan indirme tamamlandi" || bad "restart bitis" "$(fld $id state)"
# 9) izlenen klasor (canli ayar)
$f config --set "{\"watch_dirs\":[\"$S/watch\"],\"watch_done\":\"keep\"}" > /dev/null 2>&1
echo "$B/ornek-video.mp4" > $S/watch/video.list
for _ in $(seq 1 30); do [ -n "$($f list --json | python3 -c "import json,sys; print(next((j['id'] for j in json.load(sys.stdin)['jobs'] if j['filename']=='ornek-video.mp4' and j['dir']=='$S/dl'),''))")" ] && break; sleep 0.3; done
wid=$($f list --json | python3 -c "import json,sys; print(next((j['id'] for j in json.load(sys.stdin)['jobs'] if j['filename']=='ornek-video.mp4' and j['dir']=='$S/dl'),''))"); echo "$wid" >> "$MINE"
[ -n "$wid" ] && [ -f $S/watch/video.list ] && ok "izlenen klasor: dosya kuyruga alindi, yerinde birakti (keep)" || bad "izlenen klasor"
# 10) turbo <-> limit
$f limit 2MB > /dev/null 2>&1; r=$(api stats | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['turbo'], d['speed_limit'])")
[ "$r" = "False 2097152" ] && ok "ferdim limit 2MB = 2.0 MB/sn (2097152) ve turbo kapandi" || bad "limit" "$r"
$f turbo on > /dev/null 2>&1; r=$(api stats | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['turbo'], d['speed_limit'])")
[ "$r" = "True 0" ] && ok "ferdim turbo on -> limitsiz" || bad "turbo" "$r"
# 11) okunabilir hata
eid=$($f add $B/yok-bir-dosya.bin | newid); echo "$eid" >> "$MINE"; wait_done $eid
[ "$(fld $eid error)" = "kaynak bulunamadi (HTTP 404)" ] && ok "hata metni okunabilir: kaynak bulunamadi (HTTP 404)" || bad "hata metni" "$(fld $eid error)"
restore; SRV_PID=""
left=$($f list --json | python3 -c "import json,sys; print(sum(1 for j in json.load(sys.stdin)['jobs'] if j['dir']=='$S/dl'))")
[ "$left" = 0 ] && ok "temizlik: gecici dizindeki tum test isleri silindi; diger kayitlar korundu" || bad "temizlik" "$left"
echo "  (korunan diger kayitlar: $($f list --json | python3 -c "import json,sys; print([j['filename'][:38] for j in json.load(sys.stdin)['jobs'] if j['dir']!='$S/dl'])"))"
echo "SMOKE: $([ $FAIL = 0 ] && echo OK || echo FAIL)"
