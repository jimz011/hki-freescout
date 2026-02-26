"""Constants for the FreeScout integration."""

DOMAIN = "freescout"

# Configuration keys
CONF_BASE_URL = "base_url"
CONF_API_KEY = "api_key"
CONF_AGENT_ID = "agent_id"
CONF_SCAN_INTERVAL = "scan_interval"

# Defaults
DEFAULT_SCAN_INTERVAL = 60  # seconds
DEFAULT_AGENT_ID = 0  # 0 = disabled

# Sensor keys
SENSOR_OPEN = "open_tickets"
SENSOR_UNASSIGNED = "unassigned_tickets"
SENSOR_MY_TICKETS = "my_tickets"
SENSOR_NEW = "new_tickets"

# HA event fired when a new conversation is detected
EVENT_NEW_CONVERSATION = f"{DOMAIN}_new_conversation"
