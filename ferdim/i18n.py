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
    "remaining":     {"en": "left", "tr": "kaldı", "es": "restante", "ru": "осталось"},
    "unknown_size":  {"en": "unknown size", "tr": "bilinmeyen boyut", "es": "tamaño desconocido", "ru": "размер неизвестен"},
    # --------------------------------------------------------------- baslik
    "app_title":     {"en": "Download Manager", "tr": "İndirme Yöneticisi", "es": "Gestor de descargas", "ru": "Менеджер загрузок"},
    "h_new":         {"en": "New Download", "tr": "Yeni İndirme", "es": "Nueva descarga", "ru": "Новая загрузка"},
    "h_settings":    {"en": "Settings", "tr": "Ayarlar", "es": "Ajustes", "ru": "Настройки"},
    "web_ui":        {"en": "Web UI", "tr": "Web Arayüzü", "es": "Interfaz web", "ru": "Веб-интерфейс"},
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
    # ----------------------------------------------------------- baglanti durumu
    "daemon_ok":     {"en": "daemon connected", "tr": "daemon bağlı", "es": "demonio conectado", "ru": "демон подключён"},
    "daemon_down":   {"en": "daemon offline", "tr": "daemon kapalı", "es": "demonio desconectado", "ru": "демон отключён"},
    "nm_ok":         {"en": "Firefox add-on connected", "tr": "Firefox eklentisi bağlı", "es": "Extensión conectada", "ru": "Расширение подключено"},
    "nm_off":        {"en": "Firefox add-on off", "tr": "Firefox eklentisi yok", "es": "Extensión desconectada", "ru": "Расширение отключено"},
    "unlimited":     {"en": "Unlimited", "tr": "Sınırsız", "es": "Sin límite", "ru": "Без ограничений"},
    "total_speed":   {"en": "Total speed", "tr": "Toplam hız", "es": "Velocidad total", "ru": "Общая скорость"},
    "active_jobs":   {"en": "Active", "tr": "Eşzamanlı", "es": "Activas", "ru": "Активных"},
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
    "cat_other":     {"en": "file", "tr": "dosya", "es": "archivo", "ru": "файл"},
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
