# File location: /server/security/constants.py
CHAT_VIOLATION_THRESHOLD = 2

BLOCK_DURATION_MAP: dict[int, int | None] = {
    0: None,
    1: 30 * 60,
    2: 60 * 60,
    3: 6 * 60 * 60,
    4: 24 * 60 * 60,
    5: 7 * 24 * 60 * 60,
    6: None,
}

MAX_BLOCK_LEVEL = 6

REDIS_CHAT_VIOLATIONS = 'security:chat:{discussion_uuid}:violations'
REDIS_CHAT_BLOCKED = 'security:chat:{discussion_uuid}:blocked'
REDIS_USER_BLOCKED = 'security:user:{user_uuid}:blocked'
REDIS_USER_BLOCK_LEVEL = 'security:user:{user_uuid}:block_level'
REDIS_USER_BLOCK_EXPIRES = 'security:user:{user_uuid}:block_expires'

ERROR_TYPE_SECURITY_WARNING = 'security_warning'
ERROR_TYPE_SECURITY_BLOCK = 'security_block'
ERROR_TYPE_SCOPE_VIOLATION = 'scope_violation'
ERROR_TYPE_PERMISSION = 'permission'

ERROR_CODE_OFF_TOPIC_WARNING = 'off_topic_warning'
ERROR_CODE_OFF_TOPIC_BLOCK = 'off_topic_block'
ERROR_CODE_USER_PRE_BLOCKED = 'user_pre_blocked'
ERROR_CODE_CHAT_PRE_BLOCKED = 'chat_pre_blocked'
ERROR_CODE_WRONG_FEATURE = 'wrong_feature'
ERROR_CODE_NO_EDIT_ACCESS = 'no_edit_access'

WARNING_MESSAGE = (
    'This doesn\'t seem related to product development. '
    'I\'m designed to help you with feature design, product context, '
    'and technical planning. Let\'s get back on track!\n\n'
    '*Please note: continued off-topic messages may result '
    'in a temporary restriction.*'
)

BLOCK_MESSAGE_TEMPLATE = (
    'This conversation has been restricted due to repeated '
    'off-topic messages.\n\n'
    '{duration_text}\n\n'
    'Please use this tool for its intended purpose: '
    'product development and feature design.'
)

USER_BLOCKED_MESSAGE_TEMPLATE = (
    'Your account has been temporarily restricted due to '
    'repeated misuse.\n\n'
    '{duration_text}\n\n'
    'If you believe this is a mistake, please contact your '
    'organization admin.'
)

PERMANENT_BLOCK_MESSAGE = (
    'Your account has been permanently restricted due to '
    'repeated violations. Please contact support if you believe '
    'this is an error.'
)

EXPIRED_BLOCK_MESSAGE = 'This message was flagged as off-topic.'
EXPIRED_WARNING_MESSAGE = 'This message was flagged as off-topic.'
