"""Risk profiles: aggressiva (DEFAULT = current behaviour + the brain's learned rules), bilanciata, conservativa.

A profile never rewrites config or the brain's values: it is a layer applied when a value is READ.
"cap" = at most this (keeps the stricter of config/brain and profile), "floor" = at least this.
aggressiva has no layer at all, so every value is exactly the config/brain value (golden traces unchanged).
Chosen with RISK_PROFILE in .env or /aggr on Telegram (saved in data/risk_profile.json; editing .env wins)."""
import json, logging, os, time

from storage import atomic_json

log = logging.getLogger("paperbot.profiles")

DEFAULT = "aggressiva"
ALIASES = {"aggressiva": "aggressiva", "aggressivo": "aggressiva", "aggressive": "aggressiva", "a": "aggressiva",
           "bilanciata": "bilanciata", "bilanciato": "bilanciata", "balanced": "bilanciata", "b": "bilanciata",
           "conservativa": "conservativa", "conservativo": "conservativa", "conservative": "conservativa",
           "c": "conservativa"}

PROFILES = {
    "aggressiva": {"params": {}, "guards": {}, "antirug": None, "risk_mode": None},
    "bilanciata": {
        "params": {"position_size_pct": ("cap", 0.10), "max_open_positions": ("cap", 5),
                   "min_liquidity_usd": ("floor", 40000), "max_top10_pct": ("cap", 15.0),
                   "min_volume_5m_usd": ("floor", 5000), "spike_multiplier": ("floor", 10),
                   "max_entry_slippage_pct": ("cap", 8.0)},
        "guards": {"max_price_impact_pct": ("cap", 25)},
        "antirug": "shadow+top10", "risk_mode": None},
    "conservativa": {
        "params": {"position_size_pct": ("cap", 0.05), "max_open_positions": ("cap", 3),
                   "min_liquidity_usd": ("floor", 60000), "max_top10_pct": ("cap", 12.0),
                   "min_volume_5m_usd": ("floor", 8000), "spike_multiplier": ("floor", 12),
                   "max_entry_slippage_pct": ("cap", 4.0)},
        "guards": {"max_price_impact_pct": ("cap", 10), "require_jupiter_route": ("set", True)},
        "antirug": "active", "risk_mode": "conservative"},
}

LABEL = {"aggressiva": "🔥 aggressiva", "bilanciata": "⚖️ bilanciata", "conservativa": "🛡 conservativa"}


def normalize(name):
    return ALIASES.get(str(name or "").strip().lower())


def _apply(rule, base):
    kind, v = rule
    if kind == "set":
        return v
    if base is None:                         # None = no limit in config -> the profile value is the limit
        return v
    return min(base, v) if kind == "cap" else max(base, v)


def effective(profile, key, base):
    r = PROFILES.get(profile or DEFAULT, PROFILES[DEFAULT])["params"].get(key)
    return base if r is None else _apply(r, base)


def guards(profile, g):
    rules = PROFILES.get(profile or DEFAULT, PROFILES[DEFAULT])["guards"]
    if not rules:
        return g
    out = dict(g or {})
    for k, r in rules.items():
        out[k] = _apply(r, out.get(k))
    return out


def resolve(data_dir, env=os.environ):
    """(profile, source, visible). Saved /aggr choice wins unless RISK_PROFILE in .env was CHANGED after it.

    - .env RISK_PROFILE is authoritative: if it differs from the saved env value, use the new .env value.
    - Telegram profile selection (saved in risk_profile.json) is used only when .env is unchanged.
    - If RISK_PROFILE is removed from .env, the saved profile is used (user can return to default by also
      removing the saved file).
    - An unknown profile in .env falls back to DEFAULT with a warning (but NEVER to an aggressive profile).
    """
    raw = (env.get("RISK_PROFILE") or "").strip()
    envp = normalize(raw) if raw else None
    if raw and not envp:
        # Unknown profile name in .env: warn and fall back to DEFAULT (conservative), not the last aggressive profile.
        log.warning("RISK_PROFILE=%s sconosciuto: uso %s", raw, DEFAULT)
    path = os.path.join(data_dir, "risk_profile.json")
    try:
        saved = json.load(open(path, encoding="utf-8"))
    except Exception:
        saved = None
    saved_profile = normalize(saved.get("profile")) if saved else None
    saved_env = (saved.get("env") if isinstance(saved, dict) else "") or ""

    # .env RISK_PROFILE changed: it takes precedence
    if envp and envp != saved_env and not (raw == "" and saved_env == ""):
        if envp != DEFAULT:
            return envp, ".env RISK_PROFILE", True
        # Returning to the default: user explicitly cleared RISK_PROFILE to get default
        return DEFAULT, ".env RISK_PROFILE", bool(saved)

    # .env unchanged or empty: use saved profile if valid
    if saved_profile:
        # Validate the persisted profile name before trusting it
        if saved_profile in PROFILES:
            return saved_profile, "telegram /aggr", True
        else:
            log.warning("Profilo salvato '%s' non valido nel file risk_profile.json, uso %s",
                        saved_profile, DEFAULT)

    # No valid saved profile
    if envp:
        return envp, ".env RISK_PROFILE", True
    if saved:
        # Use the saved profile as a fallback when .env is cleared
        return saved_profile or DEFAULT, "telegram /aggr", True
    return DEFAULT, "predefinito", False


def save(data_dir, profile, env=os.environ):
    raw = (env.get("RISK_PROFILE") or "").strip()
    atomic_json(os.path.join(data_dir, "risk_profile.json"),
                {"profile": profile, "env": (normalize(raw) or "") if raw else "", "ts": time.time()})


def describe(profile):
    p = PROFILES[profile]
    if not p["params"]:
        return "parametri del config + regole imparate dal cervello, nessun limite in più"
    parts = []
    names = {"position_size_pct": "size", "max_open_positions": "posizioni max", "min_liquidity_usd": "liquidità min",
             "max_top10_pct": "top10 max", "min_volume_5m_usd": "volume 5m min", "spike_multiplier": "spike min",
             "max_entry_slippage_pct": "slippage entrata max"}
    for k, (kind, v) in p["params"].items():
        val = f"{100 * v:.0f}%" if k == "position_size_pct" else f"{v:g}%" if k.endswith("_pct") else \
            f"{v:,.0f} $".replace(",", ".") if k.endswith("_usd") else f"{v:g}×" if k == "spike_multiplier" else f"{v:g}"
        parts.append(f"{names[k]} {'≤' if kind == 'cap' else '≥'} {val}")
    g = p["guards"]
    if "max_price_impact_pct" in g:
        parts.append(f"impatto prezzo ≤ {g['max_price_impact_pct'][1]}%")
    if "require_jupiter_route" in g:
        parts.append("solo con quota Jupiter")
    parts.append({"shadow+top10": "AntiRug almeno shadow + blocco top10", "active": "AntiRug ACTIVE (fail-closed)"}[p["antirug"]])
    if p["risk_mode"]:
        parts.append("Risk Manager conservative")
    return ", ".join(parts)
