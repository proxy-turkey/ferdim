"""FerDim - Fedora Download Manager (dinamik, segmentli).

Segmentli coklu baglanti indirme motoru, Firefox entegrasyonu (WebExtension +
native messaging), kuyruk/planlayici, hiz limiti ve web/GTK arayuzleri.
"""
__version__ = "1.4.0"

APP_NAME = "FerDim"
EXTENSION_ID = "ferdim@ferdim.app"
NATIVE_HOST_NAME = "ferdim"

# Gercek tarayici UA'si gonderir; bazi sunucular bot UA'yi reddeder.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)
