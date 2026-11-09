"""Who is asking, and what may they see. Entitlements travel with the credential (MCP token or UI user) and are
enforced server-side in search and in every tool. Unknown callers get DEFAULT: fail closed on restricted data.

  internal      always visible (structure and meaning of the model)
  confidential  financial and other business-sensitive values
  personal      names, emails, mobiles of persons          (masked at query time; here only their definitions)
  restricted    identity numbers, credentials and similar  (agent_access none: hidden unless explicitly granted)
"""
from contextvars import ContextVar

LEVELS = ('confidential', 'personal', 'restricted')
# 'data' is a capability, not a sensitivity: may this credential query the (synthetic) database at all?
DEFAULT = {'confidential': True, 'personal': True, 'restricted': False, 'data': False}     # MCP tokens: structure only until an admin grants data
USER_DEFAULT = {**DEFAULT, 'data': True}                                                      # people logged into the UI can already browse it
FULL = {'confidential': True, 'personal': True, 'restricted': True, 'data': True}

CALLER: ContextVar = ContextVar('caller', default=('anonymous', DEFAULT))


def normalize(ent, default=None):
    ent, default = ent or {}, default or DEFAULT
    return {k: bool(ent.get(k, default[k])) for k in (*LEVELS, 'data')}


def normalize_user(ent):
    return normalize(ent, USER_DEFAULT)


def role_code(ent):
    """Database role for this combination of entitlements, e.g. c1p1r0 (confidential yes, personal yes, restricted no)."""
    ent = normalize(ent)
    return 'c%dp%dr%d' % (ent['confidential'], ent['personal'], ent['restricted'])


def set_caller(name, ent, user=False):
    return CALLER.set((name, normalize(ent, USER_DEFAULT if user else DEFAULT)))


def caller():
    return CALLER.get()


def can_see(sensitivity, agent_access, ent=None):
    ent = ent or caller()[1]
    if agent_access == 'none' and not ent['restricted']:
        return False
    if sensitivity in LEVELS and not ent[sensitivity]:
        return False
    return True


def sql_filter(ent=None):
    """(denied sensitivities, may see agent_access 'none' rows) for the search queries."""
    ent = ent or caller()[1]
    return [k for k in LEVELS if not ent[k]], bool(ent['restricted'])
