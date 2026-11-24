"""FerDim cok dilli destek: tr / en / es / ru.

- Python tarafi (CLI, GTK): t(key) ile ceviri; dil locale'den ya da
  config'deki 'language' alanindan belirlenir.
- Web arayuzu: /api/i18n?lang=xx ayni sozlugu JSON olarak dondurur.
"""
from __future__ import annotations

import locale

LANGS = ("tr", "en", "es", "ru")
LANG_NAMES = {"tr": "Türkçe", "en": "English", "es": "Español", "ru": "Русский"}

S = {
    # ------------------------------------------------------------- durumlar
    "st_queued":     {"en": "Queued", "tr": "Kuyrukta", "es": "En cola", "ru": "В очереди"},
    "st_connecting": {"en": "Connecting", "tr": "Bağlanıyor", "es": "Conectando", "ru": "Подключение"},
    "st_downloading": {"en": "Downloading", "tr": "İndiriliyor", "es": "Descargando", "ru": "Загрузка"},
    "st_paused":     {"en": "Paused", "tr": "Durduruldu", "es": "En pausa", "ru": "Приостановлено"},
    "st_done":       {"en": "Done", "tr": "Tamam", "es": "Completado", "ru": "Готово"},
    "st_error":      {"en": "Error", "tr": "Hata", "es": "Error", "ru": "Ошибка"},
    "st_canceled":   {"en": "Canceled", "tr": "İptal", "es": "Cancelado", "ru": "Отменено"},
    "st_verifying":   {"en": "Verifying", "tr": "Doğrulanıyor", "es": "Verificando", "ru": "Проверка"},
    "remaining":     {"en": "left", "tr": "kaldı", "es": "restante", "ru": "осталось"},
    "unknown_size":  {"en": "unknown size", "tr": "bilinmeyen boyut", "es": "tamaño desconocido", "ru": "размер неизвестен"},
    # --------------------------------------------------------------- baslik
    "app_title":     {"en": "Download Manager", "tr": "İndirme Yöneticisi", "es": "Gestor de descargas", "ru": "Менеджер загрузок"},
    "h_new":         {"en": "New Download", "tr": "Yeni İndirme", "es": "Nueva descarga", "ru": "Новая загрузка"},
    "h_settings":    {"en": "Settings", "tr": "Ayarlar", "es": "Ajustes", "ru": "Настройки"},
    "web_ui":        {"en": "Web UI", "tr": "Web Arayüzü", "es": "Interfaz web", "ru": "Веб-интерфейс"},
    "web_no_session": {
        "en": "This page needs a one-time session link. Open the web UI with the \u201cferdim web\u201d command, the FerDim app, or the browser extension.",
        "tr": "Bu sayfa tek kullanımlık bir oturum bağlantısı ister. Web arayüzünü \u201cferdim web\u201d komutuyla, FerDim uygulamasıyla ya da tarayıcı eklentisiyle açın.",
        "es": "Esta página necesita un enlace de sesión de un solo uso. Abre la interfaz web con el comando \u201cferdim web\u201d, la aplicación FerDim o la extensión del navegador.",
        "ru": "Этой странице нужна одноразовая ссылка сеанса. Откройте веб-интерфейс командой \u201cferdim web\u201d, в приложении FerDim или через расширение браузера."},
    # ---------------------------------------------------------------- turbo
    "turbo":         {"en": "Turbo", "tr": "Turbo", "es": "Turbo", "ru": "Турбо"},
    "turbo_on":      {"en": "TURBO ON", "tr": "TURBO AÇIK", "es": "TURBO ACTIVADO", "ru": "ТУРБО ВКЛ"},
    "turbo_off":     {"en": "TURBO OFF", "tr": "TURBO KAPALI", "es": "TURBO DESACTIVADO", "ru": "ТУРБО ВЫКЛ"},
    "turbo_title":   {"en": "Turbo: take the whole line", "tr": "Turbo: azami güç", "es": "Turbo: potencia máxima", "ru": "Турбо: полная полоса"},
    "turbo_hint": {
        "en": "Saturates your connection with up to 32 links and no limit; browsing on this line may stall. Toggle off to share again.",
        "tr": "Hattınızı 32 bağlantıya kadar ve limitsiz doyurur; bu hatte gezinti durabilir. Kapatınca paylaşmaya döner.",
        "es": "Satura tu conexión con hasta 32 enlaces sin límite; la navegación puede detenerse. Desactívalo para compartir.",
        "ru": "Насыщает канал до 32 соединениями без лимита; браузинг может остановиться. Выключите, чтобы снова делить канал."},
    "adaptive_limit": {
        "en": "server limit → {n} connections",
        "tr": "sunucu limiti → {n} bağlantı",
        "es": "límite del servidor → {n} conexiones",
        "ru": "лимит сервера → {n} соединений"},
    # --------------------------------------------------------- birlikte yasam
    "coexist":       {"en": "Coexistence", "tr": "Birlikte yaşam", "es": "Convivencia", "ru": "Совместный режим"},
    "coexist_on":    {"en": "on", "tr": "açık", "es": "activado", "ru": "вкл"},
    "coexist_off":   {"en": "off", "tr": "kapalı", "es": "desactivado", "ru": "выкл"},
    "coexist_active": {
        "en": "line busy — links reduced",
        "tr": "hat meşgul — bağlantı azaltıldı",
        "es": "línea ocupada — conexiones reducidas",
        "ru": "канал занят — соединений меньше"},
    "coexist_hint": {
        "en": "Sheds connections while other traffic is seen on the line and rebuilds them once it goes quiet. Maximum power stays the default.",
        "tr": "Hatta başka trafik görülünce bağlantıları azaltır, trafik susunca yeniden kurar. Varsayılan yine azami güçtür.",
        "es": "Reduce las conexiones cuando se ve otro tráfico en la línea y las restaura al quedar en silencio. La potencia máxima sigue siendo lo predeterminado.",
        "ru": "Снижает число соединений при чужом трафике в канале и восстанавливает их после затишья. По умолчанию по-прежнему максимальная мощность."},
    # ----------------------------------------------------------- baglanti durumu
    "daemon_ok":     {"en": "daemon connected", "tr": "daemon bağlı", "es": "demonio conectado", "ru": "демон подключён"},
    "daemon_down":   {"en": "daemon offline", "tr": "daemon kapalı", "es": "demonio desconectado", "ru": "демон отключён"},
    "nm_ok":         {"en": "Firefox add-on connected", "tr": "Firefox eklentisi bağlı", "es": "Extensión conectada", "ru": "Расширение подключено"},
    "nm_off":        {"en": "Firefox add-on off", "tr": "Firefox eklentisi yok", "es": "Extensión desconectada", "ru": "Расширение отключено"},
    "unlimited":     {"en": "Unlimited", "tr": "Sınırsız", "es": "Sin límite", "ru": "Без ограничений"},
    "total_speed":   {"en": "Total speed", "tr": "Toplam hız", "es": "Velocidad total", "ru": "Общая скорость"},
    "active_jobs":   {"en": "Active", "tr": "Eşzamanlı", "es": "Activas", "ru": "Активных"},
    # ---------------------------------------------------------------- grafikler
    "graph_title":   {"en": "Speed and connection graphs", "tr": "Hız ve bağlantı grafikleri", "es": "Gráficas de velocidad y conexiones", "ru": "Графики скорости и соединений"},
    "graph_conn":    {"en": "Connections", "tr": "Bağlantılar", "es": "Conexiones", "ru": "Соединения"},
    "graph_speed_a11y": {
        "en": "Throughput over the last {sec} s: now {cur}, peak {peak}",
        "tr": "Son {sec} sn hız grafiği: şu an {cur}, tepe {peak}",
        "es": "Rendimiento en los últimos {sec} s: ahora {cur}, máximo {peak}",
        "ru": "Скорость за последние {sec} с: сейчас {cur}, пик {peak}"},
    "graph_conn_a11y": {
        "en": "Open connections over the last {sec} s: now {cur}, peak {peak}",
        "tr": "Son {sec} sn bağlantı grafiği: şu an {cur}, tepe {peak}",
        "es": "Conexiones abiertas en los últimos {sec} s: ahora {cur}, máximo {peak}",
        "ru": "Открытые соединения за последние {sec} с: сейчас {cur}, пик {peak}"},
    "spark_a11y":    {"en": "Recent speed: {cur}", "tr": "Son hız: {cur}", "es": "Velocidad reciente: {cur}", "ru": "Недавняя скорость: {cur}"},
    "folder_label":  {"en": "FOLDER", "tr": "KLASÖR", "es": "CARPETA", "ru": "ПАПКА"},
    "sched_active":  {"en": "schedule window active", "tr": "zamanlama penceresi aktif", "es": "ventana de horario activa", "ru": "окно расписания активно"},
    # ----------------------------------------------------------------- sekmeler
    "tab_all":       {"en": "All", "tr": "Tümü", "es": "Todos", "ru": "Все"},
    "tab_video":     {"en": "Video", "tr": "Video", "es": "Vídeo", "ru": "Видео"},
    "tab_music":     {"en": "Music", "tr": "Müzik", "es": "Música", "ru": "Музыка"},
    "tab_compressed": {"en": "Compressed", "tr": "Sıkıştırılmış", "es": "Comprimidos", "ru": "Архивы"},
    "tab_program":   {"en": "Programs", "tr": "Programlar", "es": "Programas", "ru": "Программы"},
    "tab_document":  {"en": "Documents", "tr": "Belgeler", "es": "Documentos", "ru": "Документы"},
    "tab_other":     {"en": "Other", "tr": "Diğer", "es": "Otros", "ru": "Прочее"},
    "clear_finished": {"en": "Clear finished", "tr": "Tamamlananları temizle", "es": "Limpiar completadas", "ru": "Очистить завершённые"},
    # ------------------------------------------------------------------ tablo
    "col_file":      {"en": "File", "tr": "Dosya", "es": "Archivo", "ru": "Файл"},
    "col_progress":  {"en": "Progress", "tr": "İlerleme", "es": "Progreso", "ru": "Прогресс"},
    "col_size":      {"en": "Size", "tr": "Boyut", "es": "Tamaño", "ru": "Размер"},
    "col_speed":     {"en": "Speed", "tr": "Hız", "es": "Velocidad", "ru": "Скорость"},
    "col_eta":       {"en": "Left", "tr": "Kalan", "es": "Restante", "ru": "Осталось"},
    # ------------------------------------------------------------------- bos
    "empty_title":   {"en": "Queue is empty", "tr": "Kuyruk boş", "es": "La cola está vacía", "ru": "Очередь пуста"},
    "empty_hint": {
        "en": "Click a file in Firefox, use the right-click “Download with FerDim”, or add one below.",
        "tr": "Firefox'ta bir dosyaya tıklayın, “FerDim ile indir”e sağ tıklayın veya yukarıdan yeni indirme ekleyin.",
        "es": "Haz clic en un archivo en Firefox, usa el menú contextual «Descargar con FerDim» o añade uno arriba.",
        "ru": "Нажмите на файл в Firefox, используйте пункт «Скачать через FerDim» в контекстном меню или добавьте ссылку выше."},
    # ----------------------------------------------------------- ekleme penceresi
    "dlg_new":       {"en": "New Download", "tr": "Yeni İndirme", "es": "Nueva descarga", "ru": "Новая загрузка"},
    "lbl_url":       {"en": "Address", "tr": "Adres", "es": "Dirección", "ru": "Адрес"},
    "lbl_filename":  {"en": "File name", "tr": "Dosya adı", "es": "Nombre de archivo", "ru": "Имя файла"},
    "lbl_segments":  {"en": "Connections", "tr": "Bağlantı sayısı", "es": "Conexiones", "ru": "Соединений"},
    "auto":          {"en": "(automatic)", "tr": "(otomatik)", "es": "(automático)", "ru": "(авто)"},
    "btn_cancel":    {"en": "Cancel", "tr": "Vazgeç", "es": "Cancelar", "ru": "Отмена"},
    "btn_download":  {"en": "Download", "tr": "İndir", "es": "Descargar", "ru": "Скачать"},
    "start_now":     {"en": "Start immediately", "tr": "Hemen başlat", "es": "Iniciar ahora", "ru": "Начать сразу"},
    # ---------------------------------------------------------- ayar penceresi
    "lbl_dl_dir":    {"en": "Downloads folder", "tr": "İndirme dizini", "es": "Carpeta de descargas", "ru": "Папка загрузок"},
    "lbl_conc":      {"en": "Concurrent jobs", "tr": "Eşzamanlı iş sayısı", "es": "Descargas simultáneas", "ru": "Одновременных задач"},
    "lbl_sched_start": {"en": "Start time (night mode)", "tr": "Başlangıç saati (gece modu)", "es": "Hora de inicio (modo noche)", "ru": "Время начала (ночной режим)"},
    "lbl_sched_stop": {"en": "End time", "tr": "Bitiş saati", "es": "Hora de fin", "ru": "Время окончания"},
    "lbl_sched_only": {"en": "Download only in schedule window", "tr": "Sadece zamanlama penceresinde indir", "es": "Descargar solo en la ventana de horario", "ru": "Скачивать только в окне расписания"},
    "lbl_coexist":   {"en": "Shed connections while the line is busy (coexistence)", "tr": "Hat meşgulken bağlantı azalt (birlikte yaşam)", "es": "Reducir conexiones cuando la línea esté ocupada (convivencia)", "ru": "Уменьшать соединения, когда канал занят (совместный режим)"},
    "lbl_lang":      {"en": "Language / Dil", "tr": "Dil", "es": "Idioma", "ru": "Язык"},
    "lbl_speed":     {"en": "Speed limit", "tr": "Hız limiti", "es": "Límite de velocidad", "ru": "Лимит скорости"},
    "btn_save":      {"en": "Save", "tr": "Kaydet", "es": "Guardar", "ru": "Сохранить"},
    "btn_close":     {"en": "Close", "tr": "Kapat", "es": "Cerrar", "ru": "Закрыть"},
    "saved":         {"en": "saved ✓", "tr": "kaydedildi ✓", "es": "guardado ✓", "ru": "сохранено ✓"},
    # ----------------------------------------------------------------- eylemler
    "act_pause":     {"en": "Pause", "tr": "Duraklat", "es": "Pausar", "ru": "Пауза"},
    "act_resume":    {"en": "Resume", "tr": "Devam", "es": "Reanudar", "ru": "Продолжить"},
    "act_restart":   {"en": "Restart", "tr": "Baştan indir", "es": "Reiniciar", "ru": "Заново"},
    "act_remove":    {"en": "Remove", "tr": "Sil", "es": "Eliminar", "ru": "Удалить"},
    "confirm_remove": {
        "en": "Remove this job? The partial file will be deleted.",
        "tr": "İş kuyruktan silinsin mi? (yarım dosya silinir)",
        "es": "¿Eliminar la tarea? Se borrará el archivo parcial.",
        "ru": "Удалить задачу? Частичный файл будет удалён."},
    # --------------------------------------------------------------------- CLI
    "cli_added":     {"en": "added", "tr": "eklendi", "es": "añadida", "ru": "добавлено"},
    "cli_queue_empty": {"en": "queue is empty", "tr": "kuyruk boş", "es": "la cola está vacía", "ru": "очередь пуста"},
    "cli_file":      {"en": "file", "tr": "dosya", "es": "archivo", "ru": "файл"},
    "merkle_manifest": {"en": "manifest", "tr": "bildirim", "es": "manifiesto", "ru": "манифест"},
    "merkle_chunks": {"en": "chunks", "tr": "parça", "es": "fragmentos", "ru": "фрагментов"},
    "merkle_root":   {"en": "root", "tr": "kök", "es": "raíz", "ru": "корень"},
    "merkle_ok": {
        "en": "manifest verified", "tr": "bildirim doğrulandı",
        "es": "manifiesto verificado", "ru": "манифест подтверждён"},
    "merkle_bad": {
        "en": "{n} corrupted chunks", "tr": "{n} bozuk parça",
        "es": "{n} fragmentos corruptos", "ru": "{n} повреждённых фрагментов"},
    "merkle_size_bad": {
        "en": "size does not match the manifest",
        "tr": "boyut bildirimle uyuşmuyor",
        "es": "el tamaño no coincide con el manifiesto",
        "ru": "размер не совпадает с манифестом"},
    "merkle_update": {
        "en": "{reused} kept from the local file, {fetched} fetched",
        "tr": "{reused} yerelden korundu, {fetched} indirildi",
        "es": "{reused} conservados del archivo local, {fetched} descargados",
        "ru": "{reused} взято из локального файла, {fetched} загружено"},
    "cli_error":     {"en": "ERROR", "tr": "HATA", "es": "ERROR", "ru": "ОШИБКА"},
    "cli_daemon_fail": {
        "en": "FerDim daemon could not be started.",
        "tr": "HATA: FerDim daemon başlatılamadı.",
        "es": "No se pudo iniciar el demonio de FerDim.",
        "ru": "Не удалось запустить демон FerDim."},
    # kategoriler (web cipleri icin)
    "cat_video":     {"en": "video", "tr": "video", "es": "vídeo", "ru": "видео"},
    "cat_music":     {"en": "music", "tr": "müzik", "es": "música", "ru": "музыка"},
    "cat_compressed": {"en": "archive", "tr": "arşiv", "es": "archivo", "ru": "архив"},
    "cat_program":   {"en": "program", "tr": "program", "es": "programa", "ru": "программа"},
    "cat_document":  {"en": "document", "tr": "belge", "es": "documento", "ru": "документ"},
    "lbl_sha256": {"en": "SHA-256 (optional)", "tr": "SHA-256 (isteğe bağlı)", "es": "SHA-256 (opcional)", "ru": "SHA-256 (необязательно)"},
    "zip_title": {"en": "Archive contents", "tr": "Arşiv içeriği", "es": "Contenido del archivo", "ru": "Содержимое архива"},
    "zip_empty": {"en": "No entries.", "tr": "Kayıt yok.", "es": "Sin entradas.", "ru": "Нет записей."},
    "zip_extract": {"en": "Extract selected", "tr": "Seçilenleri çıkar", "es": "Extraer selección", "ru": "Извлечь выбранное"},
    "zip_repair": {"en": "Repair extraction", "tr": "Çıkarmayı onar", "es": "Reparar extracción", "ru": "Восстановить распаковку"},
    "zip_select": {"en": "Select entry", "tr": "Kaydı seç", "es": "Seleccionar entrada", "ru": "Выбрать запись"},
    "zip_sel_none": {"en": "Select at least one entry.", "tr": "En az bir kayıt seçin.", "es": "Seleccione al menos una entrada.", "ru": "Выберите хотя бы одну запись."},
    "zip_running": {"en": "Extracting… {done}/{total}", "tr": "Çıkarılıyor… {done}/{total}", "es": "Extrayendo… {done}/{total}", "ru": "Извлечение… {done}/{total}"},
    "zip_extracted": {"en": "{n} file(s) extracted to {dest}", "tr": "{n} dosya çıkarıldı: {dest}", "es": "{n} archivo(s) extraídos en {dest}", "ru": "Извлечено файлов: {n} в {dest}"},
    "zip_repaired": {"en": "{n} file(s) repaired in {dest}", "tr": "{n} dosya onarıldı: {dest}", "es": "{n} archivo(s) reparados en {dest}", "ru": "Восстановлено файлов: {n} в {dest}"},
    "zip_failed": {"en": "Failed: {e}", "tr": "Başarısız: {e}", "es": "Error: {e}", "ru": "Ошибка: {e}"},
    "zip_errors": {"en": "{n} error(s), first: {e}", "tr": "{n} hata, ilki: {e}", "es": "{n} error(es), primero: {e}", "ru": "Ошибок: {n}, первая: {e}"},
    "zip_nomf": {"en": "No extraction yet (run an extraction first).", "tr": "Henüz çıkarma yok (önce çıkarın).", "es": "Aún no hay extracción.", "ru": "Распаковки ещё не было."},
    "cat_other":     {"en": "file", "tr": "dosya", "es": "archivo", "ru": "файл"},
    # ----------------------------------------------------- saglama rozeti
    "chk_ok":        {"en": "verified: {a}", "tr": "doğrulandı: {a}", "es": "verificado: {a}", "ru": "проверено: {a}"},
    "chk_warn":      {"en": "checksum mismatch (sidecar file); file kept", "tr": "sağlama uyuşmadı (yan dosya); dosya korundu", "es": "la suma no coincide (archivo auxiliar); archivo conservado", "ru": "контрольная сумма не совпала (файл-спутник); файл сохранён"},
    "lbl_checksum":  {"en": "Checksum (optional)", "tr": "Sağlama (isteğe bağlı)", "es": "Suma de verificación (opcional)", "ru": "Контрольная сумма (необязательно)"},
    "ph_checksum":   {"en": "digest or algo:digest", "tr": "özet ya da algo:özet", "es": "suma o algo:suma", "ru": "сумма или алг:сумма"},
    "lbl_sidecar":   {"en": "Auto-verify checksums (.sha256 / .md5 sidecar files)", "tr": "Sağlamaları otomatik doğrula (.sha256 / .md5 yan dosyaları)", "es": "Verificar sumas automáticamente (archivos .sha256 / .md5)", "ru": "Автопроверка контрольных сумм (файлы .sha256 / .md5)"},
    # --------------------------------------------------- aynalar ve pencereler
    "lbl_mirrors":   {"en": "Mirrors (one URL per line, optional)", "tr": "Aynalar (her satıra bir URL, isteğe bağlı)", "es": "Espejos (una URL por línea, opcional)", "ru": "Зеркала (по одному URL в строке, необязательно)"},
    "mirrors_dead":  {"en": "{n} source(s) unavailable", "tr": "{n} kaynak erişilemez", "es": "{n} origen(es) sin acceso", "ru": "недоступно источников: {n}"},
    "lbl_cat_sched": {"en": "Per-category schedule windows", "tr": "Kategori bazlı zamanlama", "es": "Horario por categoría", "ru": "Расписание по категориям"},
    "lbl_only_window": {"en": "only in window", "tr": "sadece pencerede", "es": "solo en su ventana", "ru": "только в своём окне"},
    "sched_wait":    {"en": "waits for its window {w}", "tr": "penceresini bekliyor: {w}", "es": "espera su ventana {w}", "ru": "ждёт своего окна {w}"},
    # ------------------------------------------------------ izlenen klasorler
    "lbl_watch_dirs": {"en": "Watched folders (one per line)", "tr": "İzlenen klasörler (her satıra bir)", "es": "Carpetas vigiladas (una por línea)", "ru": "Отслеживаемые папки (по одной в строке)"},
    "lbl_watch_done": {"en": "After queueing a file", "tr": "Kuyruğa aldıktan sonra", "es": "Tras añadir a la cola", "ru": "После добавления в очередь"},
    "watch_delete":  {"en": "Delete", "tr": "Sil", "es": "Eliminar", "ru": "Удалить"},
    "watch_keep":    {"en": "Keep in place", "tr": "Yerinde bırak", "es": "Conservar", "ru": "Оставить на месте"},
    "watch_rename":  {"en": "Rename to .done", "tr": ".done olarak yeniden adlandır", "es": "Renombrar a .done", "ru": "Переименовать в .done"},
    # --------------------------------------------------------------- gelismis
    "lbl_advanced":  {"en": "Advanced", "tr": "Gelişmiş", "es": "Avanzado", "ru": "Дополнительно"},
    "lbl_retries":   {"en": "Retries", "tr": "Yeniden deneme", "es": "Reintentos", "ru": "Повторные попытки"},
    "lbl_timeout":   {"en": "Connect timeout (s)", "tr": "Bağlantı zaman aşımı (sn)", "es": "Tiempo de conexión (s)", "ru": "Таймаут соединения (с)"},
    "lbl_stall":     {"en": "Stall timeout (s)", "tr": "Akış durma sınırı (sn)", "es": "Tiempo sin datos (s)", "ru": "Таймаут остановки (с)"},
    # ------------------------------------------------------------ erisilebilirlik
    "daemon_lost":   {"en": "Connection lost", "tr": "Bağlantı koptu", "es": "Conexión perdida", "ru": "Соединение потеряно"},
    "skip_to_list":  {"en": "Skip to downloads", "tr": "İndirmelere atla", "es": "Saltar a las descargas", "ru": "Перейти к загрузкам"},
    "a11y_progress": {"en": "progress", "tr": "ilerleme", "es": "progreso", "ru": "прогресс"},
    "a11y_tabs":     {"en": "Categories", "tr": "Kategoriler", "es": "Categorías", "ru": "Категории"},
    "a11y_table":    {"en": "Downloads", "tr": "İndirmeler", "es": "Descargas", "ru": "Загрузки"},
    "done_announce": {"en": "{name}: {state}", "tr": "{name}: {state}", "es": "{name}: {state}", "ru": "{name}: {state}"},
    "cli_added_n":   {"en": "{n} added", "tr": "{n} eklendi", "es": "{n} añadidas", "ru": "добавлено: {n}"},
    "lbl_actions":    {"en": "Actions", "tr": "İşlemler", "es": "Acciones", "ru": "Действия"},
    "win_start":      {"en": "window start", "tr": "pencere başlangıcı", "es": "inicio de ventana", "ru": "начало окна"},
    "win_stop":       {"en": "window end", "tr": "pencere bitişi", "es": "fin de ventana", "ru": "конец окна"},
    "per_sec":        {"en": "/s", "tr": "/sn", "es": "/s", "ru": "/с"},
    "err_url":        {"en": "Enter a valid http(s):// or ftp(s):// address.", "tr": "Geçerli bir http(s):// ya da ftp(s):// adresi girin.", "es": "Introduce una dirección http(s):// o ftp(s):// válida.", "ru": "Введите корректный адрес http(s):// или ftp(s)://."},
    "ext_state":      {"en": "Add-on", "tr": "Eklenti", "es": "Extensión", "ru": "Расширение"},
    "add_failed":     {"en": "Could not add:", "tr": "Eklenemedi:", "es": "No se pudo añadir:", "ru": "Не удалось добавить:"},
    "warn_dir":       {"en": "Downloads folder could not be created; the default folder is used.", "tr": "İndirme dizini oluşturulamadı; varsayılan dizin kullanılıyor.", "es": "No se pudo crear la carpeta de descargas; se usa la carpeta predeterminada.", "ru": "Не удалось создать папку загрузок; используется папка по умолчанию."},
    "limit_custom":   {"en": "custom", "tr": "özel", "es": "personalizado", "ru": "свой"},
    "cli_bad_checksum": {
        "en": "invalid checksum (use algo:digest or a hex digest)",
        "tr": "geçersiz sağlama (algo:özet ya da onaltılık özet kullanın)",
        "es": "suma no válida (usa algo:suma o una suma hexadecimal)",
        "ru": "неверная контрольная сумма (алг:сумма или шестнадцатеричная сумма)"},
    # ------------------------------------------------- 1.5: hata kodlari (A)
    # ------------------------------------------------- 1.5: hiz grafigi (B)
    # ------------------------------------------------- 1.5: gecmis (C)
    # ------------------------------------------------- 1.5: gtk (D)
    # ------------------------------------------------- GTK ayar penceresi (1.5)
    "lang_auto": {
        "en": "Auto (system)",
        "tr": "Otomatik (sistem)",
        "es": "Automático (sistema)",
        "ru": "Авто (система)"},
    "err_time": {
        "en": "Enter times as HH:MM.",
        "tr": "Saatleri SS:DD biçiminde girin.",
        "es": "Introduce las horas como HH:MM.",
        "ru": "Введите время в формате ЧЧ:ММ."},
    "lang_restart": {
        "en": "The language applies to new windows; restart FerDim to refresh every label.",
        "tr": "Dil yeni pencerelerde geçerli; tüm etiketler için FerDim'i yeniden başlatın.",
        "es": "El idioma se aplica a las ventanas nuevas; reinicia FerDim para actualizar todas las etiquetas.",
        "ru": "Язык применяется к новым окнам; перезапустите FerDim, чтобы обновить все подписи."},
    "lbl_clipboard": {
        "en": "Watch the clipboard for download links",
        "tr": "Panoyu indirme bağlantıları için izle",
        "es": "Vigilar el portapapeles en busca de enlaces de descarga",
        "ru": "Следить за буфером обмена на ссылки для скачивания"},
    "save_failed": {
        "en": "Settings could not be saved:",
        "tr": "Ayarlar kaydedilemedi:",
        "es": "No se pudieron guardar los ajustes:",
        "ru": "Не удалось сохранить настройки:"},
    # ------------------------------------------- kararli hata kodlari (API)
    "err_not_found": {
        "en": "the file was not found on the server",
        "tr": "dosya sunucuda bulunamadı",
        "es": "el archivo no se encontró en el servidor",
        "ru": "файл не найден на сервере"},
    "err_forbidden": {
        "en": "the server denied access to the file",
        "tr": "sunucu dosyaya erişimi engelledi",
        "es": "el servidor denegó el acceso al archivo",
        "ru": "сервер запретил доступ к файлу"},
    "err_auth": {
        "en": "the server requires authentication",
        "tr": "sunucu kimlik doğrulama istiyor",
        "es": "el servidor requiere autenticación",
        "ru": "сервер требует аутентификации"},
    "err_source_changed": {
        "en": "the server keeps changing the file (inconsistent server)",
        "tr": "kaynak sürekli değişiyor (tutarsız sunucu)",
        "es": "el servidor cambia el archivo continuamente (servidor inconsistente)",
        "ru": "сервер постоянно меняет файл (несогласованный сервер)"},
    "err_no_sources": {
        "en": "all sources are unreachable",
        "tr": "tüm kaynaklar erişilemiyor",
        "es": "no se puede acceder a ningún origen",
        "ru": "все источники недоступны"},
    "err_checksum_mismatch": {
        "en": "checksum mismatch: the file does not match the expected digest",
        "tr": "sağlama uyuşmadı: dosya beklenen özetle eşleşmiyor",
        "es": "la suma no coincide: el archivo no coincide con la suma esperada",
        "ru": "контрольная сумма не совпала: файл не соответствует ожидаемому хешу"},
    "err_disk": {
        "en": "could not write the file to disk (space or permissions)",
        "tr": "dosya diske yazılamadı (alan ya da izin)",
        "es": "no se pudo escribir el archivo en disco (espacio o permisos)",
        "ru": "не удалось записать файл на диск (место или права)"},
    "err_network": {
        "en": "could not reach the server (network error)",
        "tr": "sunucuya ulaşılamadı (ağ hatası)",
        "es": "no se pudo acceder al servidor (error de red)",
        "ru": "не удалось связаться с сервером (сетевая ошибка)"},
    "err_stall": {
        "en": "the transfer stalled (no data for too long)",
        "tr": "aktarım durdu (uzun süre veri gelmedi)",
        "es": "la transferencia se detuvo (sin datos demasiado tiempo)",
        "ru": "передача остановилась (данные долго не поступали)"},
    "err_size_mismatch": {
        "en": "the source serves a file of a different size",
        "tr": "kaynak farklı boyutta bir dosya veriyor",
        "es": "el origen sirve un archivo de otro tamaño",
        "ru": "источник отдаёт файл другого размера"},
    "err_incompatible": {
        "en": "the source does not support resumable (range) requests",
        "tr": "kaynak aralıklı (Range) istekleri desteklemiyor",
        "es": "el origen no admite peticiones por rangos",
        "ru": "источник не поддерживает запросы диапазонами"},
    "err_rate_limited": {
        "en": "the server keeps rate-limiting the download",
        "tr": "sunucu indirmeyi sürekli sınırlıyor",
        "es": "el servidor limita la descarga continuamente",
        "ru": "сервер постоянно ограничивает загрузку"},
    "err_mirror_dead": {
        "en": "one or more mirrors are unavailable",
        "tr": "bir ya da daha çok ayna erişilemez",
        "es": "uno o más espejos no están disponibles",
        "ru": "один или несколько зеркал недоступны"},
    "err_unknown": {
        "en": "the download failed (unknown reason)",
        "tr": "indirme başarısız oldu (bilinmeyen neden)",
        "es": "la descarga falló (motivo desconocido)",
        "ru": "загрузка не удалась (неизвестная причина)"},
    "err_merkle_mismatch": {
        "en": "content-defined chunk verification failed: some byte ranges do not match the manifest",
        "tr": "parça doğrulaması başarısız: bazı bayt aralıkları bildirimle uyuşmuyor",
        "es": "la verificación por fragmentos falló: algunos rangos no coinciden con el manifiesto",
        "ru": "проверка по фрагментам не удалась: часть диапазонов не совпадает с манифестом"},
    "err_merkle_manifest": {
        "en": "the Merkle manifest could not be read or is invalid",
        "tr": "Merkle bildirimi okunamadı ya da geçersiz",
        "es": "no se pudo leer el manifiesto Merkle o no es válido",
        "ru": "манифест Merkle не прочитан или недействителен"},
    # --------------------------------------------------------------- gecmis
    "hist_title": {
        "en": "History", "tr": "Geçmiş", "es": "Historial", "ru": "История"},
    "hist_search": {
        "en": "Search history", "tr": "Geçmişte ara",
        "es": "Buscar en el historial", "ru": "Поиск в истории"},
    "hist_search_ph": {
        "en": "Search file, URL or host", "tr": "Dosya, adres ya da sunucu ara",
        "es": "Busca archivo, URL o servidor", "ru": "Искать файл, URL или сервер"},
    "hist_state": {
        "en": "State", "tr": "Durum", "es": "Estado", "ru": "Состояние"},
    "hist_all": {
        "en": "All", "tr": "Tümü", "es": "Todos", "ru": "Все"},
    "hist_clear": {
        "en": "Clear history", "tr": "Geçmişi temizle",
        "es": "Borrar historial", "ru": "Очистить историю"},
    "hist_confirm_clear": {
        "en": "Delete the whole download history?",
        "tr": "Tüm indirme geçmişi silinsin mi?",
        "es": "¿Borrar todo el historial de descargas?",
        "ru": "Удалить всю историю загрузок?"},
    "hist_empty": {
        "en": "No history yet.", "tr": "Geçmiş boş.",
        "es": "Aún no hay historial.", "ru": "История пока пуста."},
    "hist_none": {
        "en": "No matching records.", "tr": "Eşleşen kayıt yok.",
        "es": "No hay registros coincidentes.", "ru": "Совпадений нет."},
    "hist_stats": {
        "en": "{n} records · {bytes} total", "tr": "{n} kayıt · toplam {bytes}",
        "es": "{n} registros · {bytes} en total",
        "ru": "{n} записей · всего {bytes}"},
    "hist_host_line": {
        "en": "{host}: {n} · avg {links} links · {speed}",
        "tr": "{host}: {n} · ort. {links} bağlantı · {speed}",
        "es": "{host}: {n} · media {links} enlaces · {speed}",
        "ru": "{host}: {n} · сред. {links} соединений · {speed}"},
    "hist_meta": {
        "en": "{state} · {size} · {date} · {host}",
        "tr": "{state} · {size} · {date} · {host}",
        "es": "{state} · {size} · {date} · {host}",
        "ru": "{state} · {size} · {date} · {host}"},
    "hist_links": {
        "en": "avg {avg} / peak {peak} links",
        "tr": "ort. {avg} / tepe {peak} bağlantı",
        "es": "media {avg} / pico {peak} enlaces",
        "ru": "сред. {avg} / пик {peak} соединений"},
    "hist_redownload": {
        "en": "Download again", "tr": "Yeniden indir",
        "es": "Descargar de nuevo", "ru": "Скачать снова"},
    "hist_redownloaded": {
        "en": "{name} queued again.", "tr": "{name} yeniden kuyruğa alındı.",
        "es": "{name} volvió a la cola.", "ru": "{name} снова в очереди."},
    "hist_cleared": {
        "en": "{n} records deleted.", "tr": "{n} kayıt silindi.",
        "es": "{n} registros eliminados.", "ru": "{n} записей удалено."},
    "hist_days": {
        "en": "Keep history (days; 0 = forever)",
        "tr": "Geçmiş saklama (gün; 0 = sınırsız)",
        "es": "Conservar historial (días; 0 = siempre)",
        "ru": "Хранить историю (дней; 0 = всегда)"},
    "hist_failed": {
        "en": "Cannot read the history.", "tr": "Geçmiş okunamadı.",
        "es": "No se pudo leer el historial.",
        "ru": "Не удалось прочитать историю."},
    # ------------------------------------------- ogrenilen ana makine profilleri
    "hosts_row": {
        "en": "{host}  {links} links  rtt {rtt}  {speed}/s per link  ({samples} samples, {seen})",
        "tr": "{host}  {links} bağlantı  rtt {rtt}  bağlantı başına {speed}/sn  ({samples} örnek, {seen})",
        "es": "{host}  {links} enlaces  rtt {rtt}  {speed}/s por enlace  ({samples} muestras, {seen})",
        "ru": "{host}  {links} соединений  rtt {rtt}  {speed}/с на соединение  ({samples} замеров, {seen})"},
    "hosts_title": {
        "en": "Hosts", "tr": "Ana makineler",
        "es": "Servidores", "ru": "Серверы"},
    "hosts_forget_all": {
        "en": "Forget all", "tr": "Tümünü unut",
        "es": "Olvidar todo", "ru": "Забыть всё"},
    "hosts_empty": {
        "en": "No learned host profiles yet.",
        "tr": "Henüz öğrenilmiş ana makine profili yok.",
        "es": "Aún no hay perfiles de servidor aprendidos.",
        "ru": "Изученных профилей серверов пока нет."},
    "hosts_forgotten": {
        "en": "Forgot {host}.", "tr": "{host} unutuldu.",
        "es": "Se olvidó {host}.", "ru": "{host} забыт."},
    "hosts_unknown": {
        "en": "No profile for {host}.", "tr": "{host} için profil yok.",
        "es": "No hay perfil para {host}.",
        "ru": "Нет профиля для {host}."},
    "hosts_cleared": {
        "en": "{n} host profiles deleted.",
        "tr": "{n} ana makine profili silindi.",
        "es": "{n} perfiles de servidor eliminados.",
        "ru": "Удалено профилей серверов: {n}."},
    "rtt_low": {
        "en": "low", "tr": "düşük", "es": "baja", "ru": "низкий"},
    "rtt_mid": {
        "en": "mid", "tr": "orta", "es": "media", "ru": "средний"},
    "rtt_high": {
        "en": "high", "tr": "yüksek", "es": "alta", "ru": "высокий"},
    # ------------------------------------------------------- masaustu bildirimi
    "notify_done_title": {
        "en": "Download complete", "tr": "İndirme tamamlandı",
        "es": "Descarga completada", "ru": "Загрузка завершена"},
    "notify_error_title": {
        "en": "Download failed", "tr": "İndirme başarısız",
        "es": "Descarga fallida", "ru": "Загрузка не удалась"},
    "notify_verify_title": {
        "en": "Verification result", "tr": "Doğrulama sonucu",
        "es": "Resultado de verificación", "ru": "Результат проверки"},
    "notify_chk_ok": {
        "en": "checksum verified", "tr": "sağlama doğrulandı",
        "es": "suma verificada", "ru": "контрольная сумма подтверждена"},
    "notify_chk_warn": {
        "en": "sidecar checksum mismatch; file kept",
        "tr": "yan dosya sağlaması uyuşmadı; dosya korundu",
        "es": "la suma del archivo auxiliar no coincide; archivo conservado",
        "ru": "сумма файла-спутника не совпала; файл сохранён"},
    "notify_chk_mismatch": {
        "en": "checksum mismatch", "tr": "sağlama uyuşmadı",
        "es": "la suma no coincide",
        "ru": "контрольная сумма не совпала"},
    "notify_summary_title": {
        "en": "{n} downloads finished", "tr": "{n} indirme tamamlandı",
        "es": "{n} descargas terminadas", "ru": "{n} загрузок завершено"},
    "notify_summary_body": {
        "en": "{done} completed, {failed} failed",
        "tr": "{done} tamam, {failed} hata",
        "es": "{done} completadas, {failed} fallidas",
        "ru": "{done} завершено, {failed} с ошибкой"},
    "notify_summary_warn": {
        "en": "{n} with a checksum warning",
        "tr": "{n} sağlama uyarısı",
        "es": "{n} con aviso de suma",
        "ru": "{n} с предупреждением о сумме"},
    # ------------------------------------------------------------ gtk 1.7
    # hosts_title / hosts_forget_all web seridinde zaten tanimli
    "hosts_forget": {
        "en": "Forget", "tr": "Unut", "es": "Olvidar", "ru": "Забыть"},
    "hosts_confirm_clear": {
        "en": "Forget every learned host profile?",
        "tr": "Öğrenilmiş tüm ana makine profilleri unutulsun mu?",
        "es": "¿Olvidar todos los perfiles de servidor aprendidos?",
        "ru": "Забыть все изученные профили серверов?"},
    "chk_merkle_warn": {
        "en": "Merkle manifest mismatch (automatic); file kept",
        "tr": "Merkle bildirimi uyuşmadı (otomatik); dosya korundu",
        "es": "el manifiesto Merkle no coincide (automático); archivo conservado",
        "ru": "манифест Merkle не совпал (автоматически); файл сохранён"},
    # ---- 1.8 speed/metrics
    "plan_title": {
        "en": "Speed plan", "tr": "Hız planı",
        "es": "Plan de velocidad", "ru": "План скорости"},
    "plan_json": {
        "en": "Plan (JSON list of rules)",
        "tr": "Plan (JSON kural listesi)",
        "es": "Plan (lista JSON de reglas)",
        "ru": "План (список правил JSON)"},
    "plan_rule": {
        "en": "{days} · {hours} · {limit}",
        "tr": "{days} · {hours} · {limit}",
        "es": "{days} · {hours} · {limit}",
        "ru": "{days} · {hours} · {limit}"},
    "plan_every_day": {
        "en": "every day", "tr": "her gün",
        "es": "todos los días", "ru": "каждый день"},
    "plan_all_day": {
        "en": "all day", "tr": "tüm gün",
        "es": "todo el día", "ru": "весь день"},
    "plan_unlimited": {
        "en": "unlimited", "tr": "sınırsız",
        "es": "sin límite", "ru": "без лимита"},
    "plan_state_active": {
        "en": "Active now: {limit}", "tr": "Şu an etkin: {limit}",
        "es": "Activo ahora: {limit}", "ru": "Сейчас действует: {limit}"},
    "plan_state_off": {
        "en": "No rule matches now — maximum power.",
        "tr": "Şu an hiçbir kural eşleşmiyor — azami güç.",
        "es": "Ninguna regla coincide ahora — potencia máxima.",
        "ru": "Сейчас ни одно правило не подходит — полная мощность."},
    "plan_state_manual": {
        "en": "Manual limit override: the plan is paused until "
              "'ferdim speedplan resume' (or 'ferdim limit plan').",
        "tr": "Elle verilen limit planı eziyor: 'ferdim speedplan resume' "
              "(ya da 'ferdim limit plan') denetimi plana bırakır.",
        "es": "Límite manual: el plan está en pausa hasta "
              "'ferdim speedplan resume' (o 'ferdim limit plan').",
        "ru": "Ручной лимит: план приостановлен до "
              "'ferdim speedplan resume' (или 'ferdim limit plan')."},
    "plan_state_none": {
        "en": "No speed plan.", "tr": "Hız planı yok.",
        "es": "No hay plan de velocidad.", "ru": "Плана скорости нет."},
    "plan_bad": {
        "en": "Invalid plan JSON: expected a list of rule objects.",
        "tr": "Geçersiz plan JSON: kural nesnelerinden oluşan liste bekleniyor.",
        "es": "JSON de plan inválido: se espera una lista de reglas.",
        "ru": "Неверный JSON плана: ожидается список правил."},
    "plan_saved": {
        "en": "Speed plan saved.", "tr": "Hız planı kaydedildi.",
        "es": "Plan de velocidad guardado.", "ru": "План скорости сохранён."},
    "plan_cleared": {
        "en": "Speed plan cleared.", "tr": "Hız planı silindi.",
        "es": "Plan de velocidad eliminado.", "ru": "План скорости удалён."},
    "plan_resumed": {
        "en": "The speed plan is in control again.",
        "tr": "Denetim yeniden hız planında.",
        "es": "El plan de velocidad vuelve a mandar.",
        "ru": "Управление снова у плана скорости."},
    # ---- 2.x peer/tcpinfo
    "congestion_limit": {
        "en": "{n} links (network congestion)",
        "tr": "{n} bağlantı (ağ tıkanıklığı)",
        "es": "{n} enlaces (congestión de red)",
        "ru": "{n} соединений (перегрузка сети)"},
    "peer_cache": {
        "en": "LAN peer cache",
        "tr": "LAN eşbaz önbelleği",
        "es": "Caché de pares en LAN",
        "ru": "Кэш узлов в локальной сети"},
    # ---- launcher web oturumu (tek kullanimlik URL)
    "launch_web_disabled": {
        "en": "The web UI is disabled in the configuration.",
        "tr": "Web arayüzü yapılandırmada kapalı.",
        "es": "La interfaz web está desactivada en la configuración.",
        "ru": "Веб-интерфейс отключён в настройках."},
    "launch_web_failed": {
        "en": "Could not obtain a web UI address.",
        "tr": "Web arayüzü adresi alınamadı.",
        "es": "No se pudo obtener una dirección de la interfaz web.",
        "ru": "Не удалось получить адрес веб-интерфейса."},
    "launch_token_rotated": {
        "en": "API token rotated; open a fresh web UI session.",
        "tr": "API jetonu yenilendi; web arayüzünü yeniden açın.",
        "es": "Jeton de API renovado; abra una sesión nueva de la interfaz web.",
        "ru": "Токен API обновлён; откройте веб-интерфейс заново."},
    "launch_token_failed": {
        "en": "Could not rotate the API token.",
        "tr": "API jetonu yenilenemedi.",
        "es": "No se pudo renovar el jeton de API.",
        "ru": "Не удалось обновить токен API."},
}


def detect_lang(cfg_lang: str | None = None) -> str:
    """Oncelik: config -> sistem locale -> 'tr'."""
    cand = (cfg_lang or "").strip().lower()
    if cand in LANGS:
        return cand
    if cand and cand != "auto":
        prefix = cand.split("_")[0]
        if prefix in LANGS:
            return prefix
    try:
        loc = locale.getdefaultlocale()[0] or ""
    except Exception:
        loc = ""
    prefix = loc.split("_")[0].lower()
    return prefix if prefix in LANGS else "tr"


def t(key: str, lang: str | None = None, **kw) -> str:
    lang = lang or detect_lang()
    entry = S.get(key)
    if not entry:
        return key
    txt = entry.get(lang) or entry.get("en") or key
    if kw:
        try:
            txt = txt.format(**kw)
        except Exception:
            pass
    return txt


def dict_for(lang: str) -> dict:
    lang = lang if lang in LANGS else "tr"
    return {k: (v.get(lang) or v.get("en") or k) for k, v in S.items()}


def error_text(code: str, fallback: str = "", lang: str | None = None) -> str:
    """Arayuzler icin: kararli hata/warning kodu varsa cevirisi, yoksa
    motorun ham (Turkce) mesaji. Sozlukte olmayan kod ham metne duser."""
    key = f"err_{code}" if code else ""
    if key and key in S:
        return t(key, lang)
    return fallback
