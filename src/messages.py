from typing import Final

# availability
AT_CAPACITY: Final = "All servers are at capacity right now. Please try again in a few minutes."
NO_HEALTHY_HOSTS: Final = "no healthy docker contexts available"
SERVER_BUSY: Final = "server busy, please try again shortly"
HOST_UNREACHABLE: Final = "the session host is temporarily unreachable, please try again shortly"
SETTINGS_INVALID: Final = "Remote Desktop settings are invalid; contact an administrator"

# access and limits
FEATURE_DISABLED: Final = "Remote Desktop is currently disabled"
EMAIL_VERIFICATION_REQUIRED: Final = "Email verification required"
SESSION_SUSPENDED: Final = "Session suspended, contact an administrator"
MAX_EXTENSIONS: Final = "Maximum extensions reached"
RATE_LIMITED: Final = "Too many requests. Limit is {limit} requests in {interval} seconds"

# lifecycle
SESSION_ALREADY_EXISTS: Final = "Session already exists"
CREATE_IN_PROGRESS: Final = "Session creation already in progress"
CREATE_ALREADY_RUNNING: Final = "Creation already in progress"
LIFECYCLE_BUSY: Final = "Session creation or cleanup already in progress"
NOT_DESTROYABLE: Final = "Session is not in a destroyable state"
SESSION_STOPPING: Final = "Session is stopping or suspended"
SESSION_CHANGED: Final = "Session changed, please refresh and try again"
STATE_UNKNOWN: Final = "Container state is unknown; refusing destructive cleanup"
STOP_OUTCOME_UNKNOWN: Final = "Container stop outcome is unknown; cleanup will be retried"
CREDENTIAL_REVOCATION_PENDING: Final = "Container stopped; session credential revocation will be retried"
NOT_READY_IN_TIME: Final = "the session did not become ready in time, please try again"

# not found and validation
NO_ACTIVE_SESSION: Final = "No active session"
NO_ACTIVE_CONTAINER: Final = "No active container found"
TIMER_NOT_STARTED: Final = "Timer not started"
INVALID_REQUEST: Final = "invalid request"
REPORT_EMPTY: Final = "Report cannot be empty"
REPORT_TOO_LONG: Final = "Report is too long (5000 char max)"
# the braces are literal, never run this through str.format
CONFIRM_REQUIRED: Final = 'confirmation required: post body must contain {"confirm": "DELETE"}'

# generic
SERVER_ERROR: Final = "Something went wrong on our end, please try again later or contact an administrator."
FEATURE_DISABLED_PAGE: Final = "This feature has been disabled by an administrator. Check back later."
EMAIL_VERIFICATION_PAGE: Final = "You need to verify your email address before you can access Remote Desktop."
