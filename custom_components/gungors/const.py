"""Constants for the gungors integration."""

DOMAIN = "gungors"

CONF_FEATURES = "features"

# Fired by the pushbutton blueprint for covers with the `buttons` feature.
# event_data: {entity_id: str | list[str], action: "open" | "close"}
EVENT_PHYSICAL_COVER = "gungors_physical_cover"

# --- cover -----------------------------------------------------------------------
ATTR_ACTUAL_POSITION = "actual_position"
ATTR_PHYSICAL_BUTTONS = "physical_buttons"
ATTR_PENDING_POSITION = "pending_position"

# Position reports within this tolerance of the target count as "reached".
POSITION_TOLERANCE = 1

# Zigbee2MQTT echoes the *target* position right after a set_cover_position
# command, before the blind has moved. Real movement is ~2-3 units per report
# (one per second), so a jump larger than this straight to the commanded target
# is that echo.
MAX_REAL_STEP = 5

DEFAULT_START_TIMEOUT = 8  # s to wait for the first report after a command
DEFAULT_STOP_SILENCE = 3  # s without reports before a move counts as stopped

DEFAULT_TRAVEL_TIME = 10  # initial full-run time (s) per direction, then learned
MIN_LEARN_DISTANCE = 30  # % a move must cover to update the learned time
DEFAULT_Z2M_BASE_TOPIC = "zigbee2mqtt"
ATTR_CALIBRATED = "calibrated"
ATTR_POSITION_SOURCE = "position_source"  # "reported" | "estimated"
ATTR_OPEN_TIME = "open_time"  # learned full-run times (s), room frame
ATTR_CLOSE_TIME = "close_time"

# --- climate ---------------------------------------------------------------------
ATTR_PHYSICAL_THERMOSTAT = "physical_thermostat"
ATTR_PRE_OFF_TARGET_TEMP = "pre_off_target_temp"

# How long (s) events from the physical thermostat are treated as echoes of a
# command we just sent.
PUSH_ECHO_TIMEOUT = 15
# When the physical thermostat is switched on from OFF, wait (s) for it to report
# the new mode before writing the setpoint (EMS-ESP stores a setpoint received
# while OFF as its "off temperature").
PHYSICAL_MODE_WAIT = 10
# Minimum temperature difference (°C) considered a real change.
TEMP_TOLERANCE = 0.05

# --- hold ------------------------------------------------------------------------
ATTR_HOLD = "hold"
HOLD_STRICT = "strict"  # every deviation is rejected or pushed back
HOLD_MANUAL = "manual"  # the state is set at the start, changes are allowed
