"""Constants for the eufy Clean (Local) integration."""

DOMAIN = "eufy_clean_local"

CONF_DEVICE_ID = "device_id"
CONF_LOCAL_KEY = "local_key"
CONF_MODEL = "model"
CONF_MAC = "mac"
CONF_HOST = "host"
CONF_MAP_INTERVAL = "map_interval"
CONF_ROOMS = "rooms"
CONF_LIVE_INTERVAL = "live_interval"

DEFAULT_MAP_INTERVAL = 120  # seconds between cloud map refreshes while cleaning
DEFAULT_LIVE_INTERVAL = 3  # seconds between robot-position reads while cleaning (0 = off)
LIVE_STATES = ("cleaning", "returning")
