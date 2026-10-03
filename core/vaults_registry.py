# -*- coding: utf-8 -*-
"""
Vault-Registry (Kabelsalat-SoT) — Adapter auf bind_store.

Nutzer:   ~/.glyph/vaults.json
Produkt:  ~/.glyph/vaults.defaults.json (leer/minimal, nie Nutzer-Welt überschreiben)

Hot-Reload: load() liest Datei bei jedem Aufruf (mtime-Cache optional kurz).
"""
from __future__ import annotations

import json
import os
import uuid
from typing import List, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import bind_store, log

GLYPH_DIR = os.path.expanduser("~/.glyph")
USER_STORE = os.path.join(GLYPH_DIR, "vaults.json")
DEFAULTS_STORE = os.path.join(GLYPH_DIR, "vaults.defaults.json")
OBSIDIAN_ROOT = os.path.expanduser("~/ObsidianVaults")

EXTRA = ("pins",)
FLAGS = dict(reorder=True, require_exists=False, home_head="agent")

# Seed nur wenn vaults.json fehlt (bestehende Installation migrieren)
_LEGACY_SEED = [
    {
        "name": "HSEQ Sync",
        "path": os.path.join(OBSIDIAN_ROOT, "HSEQ Sync"),
        "mode": "rw",
        "primary": True,
    },
    {
        "name": "ASI, BS. UWS, QM, EM",
        "path": os.path.join(OBSIDIAN_ROOT, "ASI, BS. UWS, QM, EM"),
        "mode": "r",
        "primary": False,
    },
    {
        "name": "memory-wiki",
        "path": os.path.join(OBSIDIAN_ROOT, "memory-wiki"),
        "mode": "rw",
        "primary": False,
    },
    {
        "name": "Peniel",
        "path": os.path.join(OBSIDIAN_ROOT, "Peniel"),
        "mode": "private",
        "primary": False,
    },
    {
        "name": "Privat",
        "path": os.path.join(OBSIDIAN_ROOT, "Privat"),
        "mode": "private",
        "primary": False,
    },
]

# Default-Pins (nutzerunabhängig) — relative Pfade im Vault
_DEFAULT_PIN_CANDIDATES = (
    "AGENTS.md",
    "00 MOC - Start.md",
    "00 MOC - Sync Start.md",
    "WIKI.md",
    "index.md",
    "MEMORY.md",
)


def _slug(name: str) -> str:
    return bind_store.slug(name, fallback="vault")


def _empty_store() -> dict:
    return {"version": 1, "vaults": []}


def ensure_defaults_file() -> None:
    os.makedirs(GLYPH_DIR, exist_ok=True)
    if not os.path.isfile(DEFAULTS_STORE):
        with open(DEFAULTS_STORE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "version": 1,
                    "description": "Produkt-Default: leere Vault-Liste. Nutzer-Welt: vaults.json",
                    "vaults": [],
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
            f.write("\n")


def _sanitize_pins(pins_in) -> list:
    pins = []
    seen_p = set()
    for p in pins_in or []:
        if not isinstance(p, dict):
            continue
        rel = str(p.get("path") or "").strip().replace("\\", "/").lstrip("/")
        if not rel or rel in seen_p:
            continue
        seen_p.add(rel)
        pins.append(
            {
                "path": rel,
                "label": str(p.get("label") or os.path.basename(rel)),
                "source": str(p.get("source") or "manual"),
            }
        )
    return pins


def load_store(*, force: bool = False) -> dict:
    """Lädt Nutzer-SoT; migriert einmalig wenn Datei fehlt."""
    ensure_defaults_file()
    os.makedirs(GLYPH_DIR, exist_ok=True)

    if not os.path.isfile(USER_STORE):
        store = _migrate_seed()
        return save_store(store)

    store = bind_store.load_store(
        USER_STORE, "vaults", extra_keys=EXTRA, force=force, **FLAGS
    )
    if bind_store.apply_heads_schema(store, "vaults", home_head="agent"):
        return save_store(store)
    return store


def _migrate_seed() -> dict:
    """Bestehende Welt: Legacy-Pfade. Frische Installation ohne Ordner → leere Liste."""
    vaults = []
    any_exist = False
    for i, raw in enumerate(_LEGACY_SEED):
        if os.path.isdir(os.path.expanduser(raw["path"])):
            any_exist = True
        v = bind_store.normalize_item(
            {**raw, "id": _slug(raw["name"]), "pins": []},
            i,
            extra_keys=EXTRA,
            home_head="agent",
        )
        if v and v["exists"]:
            v["pins"] = _auto_pins_for_path(v["path"])
            vaults.append(v)
    if not any_exist:
        return _empty_store()
    log.log("vaults_migrated", count=len(vaults))
    return {"version": 1, "vaults": vaults, "migrated_from": "legacy_config"}


def save_store(data: dict) -> dict:
    extra_meta = {}
    if data.get("migrated_from"):
        extra_meta["migrated_from"] = data["migrated_from"]
    if data.get("heads_schema"):
        extra_meta["heads_schema"] = data["heads_schema"]
    out = bind_store.save_store(
        USER_STORE,
        data,
        "vaults",
        extra_keys=EXTRA,
        extra_meta=extra_meta or None,
        **FLAGS,
    )
    log.log("vaults_saved", count=len(out.get("vaults") or []))
    return out


def list_vaults() -> List[dict]:
    return list(load_store().get("vaults") or [])


def get_vault(vid: str) -> Optional[dict]:
    for v in list_vaults():
        if v["id"] == vid:
            return v
    return None


def _agent_mode(item: dict, *, home_head: str) -> str:
    return bind_store.head_mode(item, "agent", home_head=home_head)


def paths_for_agent() -> List[str]:
    """Enabled folders bound to °_Agent. Private included (read)."""
    out = []
    seen = set()
    for v in list_vaults():
        if not v.get("enabled", True):
            continue
        if _agent_mode(v, home_head="agent") == "unbound":
            continue
        if not v.get("exists") and not os.path.isdir(v["path"]):
            continue
        out.append(v["path"])
        seen.add(v["path"])
    try:
        from . import workspaces_registry as wr

        for w in wr.list_workspaces(include_missing=False):
            if not w.get("enabled", True):
                continue
            if _agent_mode(w, home_head="code") == "unbound":
                continue
            p = w["path"]
            if p in seen:
                continue
            out.append(p)
            seen.add(p)
    except Exception:
        pass
    return out


def private_paths() -> List[str]:
    out = [
        v["path"]
        for v in list_vaults()
        if v.get("enabled", True)
        and _agent_mode(v, home_head="agent") == "private"
    ]
    seen = set(out)
    try:
        from . import workspaces_registry as wr

        for w in wr.list_workspaces(include_missing=False):
            if not w.get("enabled", True):
                continue
            if _agent_mode(w, home_head="code") != "private":
                continue
            if w["path"] in seen:
                continue
            out.append(w["path"])
            seen.add(w["path"])
    except Exception:
        pass
    return out


def writable_paths() -> List[str]:
    out = [
        v["path"]
        for v in list_vaults()
        if v.get("enabled", True) and _agent_mode(v, home_head="agent") == "rw"
    ]
    seen = set(out)
    try:
        from . import workspaces_registry as wr

        for w in wr.list_workspaces(include_missing=False):
            if not w.get("enabled", True):
                continue
            if _agent_mode(w, home_head="code") != "rw":
                continue
            if w["path"] in seen:
                continue
            out.append(w["path"])
            seen.add(w["path"])
    except Exception:
        pass
    return out


def _longest_root(real: str, roots) -> str:
    """Der spezifischste (längste) Root, unter dem `real` liegt — oder ""."""
    best = ""
    for p in roots:
        try:
            root = os.path.realpath(p)
        except OSError:
            root = p
        if (real == root or real.startswith(root + os.sep)) and len(root) > len(best):
            best = root
    return best


def is_private_path(abs_path: str) -> bool:
    """True, wenn der SPEZIFISCHSTE zuständige Vault privat ist.

    Nicht „irgendein Treffer": das Home-Verzeichnis ist als privater Vault
    registriert und liegt damit über allen anderen Vaults. Mit der alten
    Any-Match-Regel galt dadurch JEDER Vault-Pfad als privat — Schreiben war
    überall gesperrt („Privat-Vault: Schreiben gesperrt"), auch in „HSEQ Sync"
    mit mode=rw. Deshalb entscheidet der längste passende Root.
    """
    try:
        real = os.path.realpath(abs_path)
    except OSError:
        real = abs_path
    priv = _longest_root(real, private_paths())
    if not priv:
        return False
    other = _longest_root(real, writable_paths())
    return not (other and len(other) > len(priv))


def is_writable_path(abs_path: str) -> bool:
    try:
        real = os.path.realpath(abs_path)
    except OSError:
        real = abs_path
    for p in writable_paths():
        try:
            root = os.path.realpath(p)
        except OSError:
            root = p
        if real == root or real.startswith(root + os.sep):
            return True
    return False


def apply_to_config() -> List[str]:
    """Schreibt live in config.VAULT_PATHS / VAULT_PATH."""
    from . import config

    paths = paths_for_agent()
    if not paths:
        config.VAULT_PATHS = []
        config.VAULT_PATH = ""
    else:
        config.VAULT_PATHS = paths
        config.VAULT_PATH = paths[0]
    return paths


def parse_attach_input(raw: str) -> dict:
    """
    Pfad | obsidian:// | Vault-Name unter ~/ObsidianVaults/
    → {path, name, pin_file?}
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("Leere Eingabe")

    pin_file = None
    name = None
    path = None

    if s.startswith("obsidian://"):
        # obsidian://open?vault=HSEQ%20Sync&file=00%20MOC%20-%20Sync%20Start
        u = urlparse(s)
        qs = parse_qs(u.query)
        vault = unquote((qs.get("vault") or [""])[0])
        file_q = unquote((qs.get("file") or [""])[0])
        if not vault:
            raise ValueError("obsidian:// ohne vault=")
        name = vault
        path = os.path.join(OBSIDIAN_ROOT, vault)
        if file_q:
            pin_file = file_q.replace("\\", "/")
            if not pin_file.endswith(".md"):
                pin_file = pin_file + ".md"
    elif os.path.isabs(os.path.expanduser(s)) or s.startswith("~"):
        path = os.path.realpath(os.path.expanduser(s))
        name = os.path.basename(path)
    else:
        # vault name under ObsidianVaults
        name = s
        path = os.path.join(OBSIDIAN_ROOT, s)
        path = os.path.realpath(path)

    if not os.path.isdir(path):
        raise ValueError(f"Ordner nicht gefunden: {path}")

    return {"path": path, "name": name, "pin_file": pin_file}


def _auto_pins_for_path(vault_path: str) -> List[dict]:
    pins = []
    for rel in _DEFAULT_PIN_CANDIDATES:
        full = os.path.join(vault_path, rel)
        if os.path.isfile(full):
            pins.append(
                {
                    "path": rel,
                    "label": os.path.basename(rel),
                    "source": "default",
                }
            )
    return pins


def ensure_default_mds(vault_path: str) -> List[str]:
    """AGENTS.md, 00 MOC - Start.md, MEMORY.md (leer/Sektion) wenn fehlend."""
    created = []
    agents = os.path.join(vault_path, "AGENTS.md")
    if not os.path.isfile(agents):
        with open(agents, "w", encoding="utf-8") as f:
            f.write(
                "# AGENTS.md\n\n"
                "Kurzvertrag für diesen Vault (Glyph °_Agent).\n\n"
                "## Rolle\n"
                "_(Was dieser Vault ist — 2–5 Sätze)_\n\n"
                "## Tabus\n"
                "- Keine erfundenen Fakten\n"
                "- Bei Privat-Modus: kein Cloud-Korpus aus diesem Vault\n"
            )
        created.append("AGENTS.md")

    moc = os.path.join(vault_path, "00 MOC - Start.md")
    if not os.path.isfile(moc):
        # don't overwrite Sync Start if only that exists
        alt = os.path.join(vault_path, "00 MOC - Sync Start.md")
        if not os.path.isfile(alt):
            name = os.path.basename(vault_path)
            with open(moc, "w", encoding="utf-8") as f:
                f.write(
                    f"---\ntags: [moc, start]\ntype: moc\n---\n\n"
                    f"# {name} · Start\n\n"
                    f"Hub für diesen Vault. Links zu wichtigen Notizen hier sammeln.\n\n"
                    f"## Einstiege\n\n- [[AGENTS]]\n- [[MEMORY]]\n"
                )
            created.append("00 MOC - Start.md")

    mem = os.path.join(vault_path, "MEMORY.md")
    if not os.path.isfile(mem):
        with open(mem, "w", encoding="utf-8") as f:
            f.write(
                "# MEMORY (Vault)\n\n"
                "Lokale Langzeit-Notizen **dieses** Vaults.\n"
                "Zentrale Glyph-Memory: `~/.glyph/MEMORY.md` (nicht hier mischen).\n\n"
                "## Notizen\n\n"
            )
        created.append("MEMORY.md")
    return created


def attach(raw_input: str, mode: str = "r", head: Optional[str] = None) -> dict:
    parsed = parse_attach_input(raw_input)
    mode = bind_store.normalize_mode(mode or "r")
    head_id = str(head or "").strip().lower() or None
    if head_id and head_id not in bind_store.HEADS:
        head_id = None
    store = load_store(force=True)
    path = parsed["path"]
    for v in store.get("vaults") or []:
        if os.path.realpath(v.get("path") or "") == path:
            if head_id:
                return {
                    "vault": update_vault(v["id"], {"heads": {head_id: mode}}),
                    "created_mds": [],
                }
            raise ValueError("Vault bereits angebunden")

    created_mds = ensure_default_mds(path)
    pins = _auto_pins_for_path(path)
    if parsed.get("pin_file"):
        rel = parsed["pin_file"]
        if not any(p["path"] == rel for p in pins):
            pins.append({"path": rel, "label": os.path.basename(rel), "source": "manual"})

    home_mode = mode if (not head_id or head_id == "agent") else "unbound"
    heads = bind_store.default_heads("agent", home_mode if home_mode != "unbound" else "r")
    if home_mode == "unbound":
        heads["agent"] = "unbound"
    if head_id:
        heads[head_id] = mode

    vault = bind_store.normalize_item(
        {
            "id": _slug(parsed["name"]) + "-" + uuid.uuid4().hex[:4],
            "name": parsed["name"],
            "path": path,
            "mode": mode if home_mode != "unbound" else mode,
            "heads": heads,
            "primary": len(store.get("vaults") or []) == 0,
            "pins": pins,
            "enabled": True,
        },
        len(store.get("vaults") or []),
        extra_keys=EXTRA,
        home_head="agent",
    )
    if not vault:
        raise ValueError("Vault konnte nicht angelegt werden")
    store.setdefault("vaults", []).append(vault)
    save_store(store)
    apply_to_config()
    return {"vault": get_vault(vault["id"]) or vault, "created_mds": created_mds}


def detach(vid: str) -> bool:
    ok = bind_store.detach_item(
        USER_STORE, "vaults", vid, extra_keys=EXTRA, **FLAGS
    )
    if ok:
        apply_to_config()
        log.log("vaults_saved", count=len(list_vaults()))
    return ok


def update_vault(vid: str, patch: dict) -> dict:
    bind_store.update_item(
        USER_STORE, "vaults", vid, patch, extra_keys=EXTRA, **FLAGS
    )
    if "pins" in (patch or {}) and isinstance(patch["pins"], list):
        store = load_store(force=True)
        for v in store.get("vaults") or []:
            if v.get("id") == vid:
                v["pins"] = _sanitize_pins(patch["pins"])
                break
        save_store(store)
    else:
        log.log("vaults_saved", count=len(list_vaults()))
    apply_to_config()
    item = get_vault(vid)
    if not item:
        raise ValueError("Vault nicht gefunden")
    return item


def add_pin(vid: str, rel_path: str, label: str = "") -> dict:
    rel = (rel_path or "").strip().replace("\\", "/").lstrip("/")
    if not rel:
        raise ValueError("pin path fehlt")
    store = load_store(force=True)
    for v in store.get("vaults") or []:
        if v.get("id") != vid:
            continue
        pins = list(v.get("pins") or [])
        if any(p.get("path") == rel for p in pins):
            return v
        pins.append(
            {
                "path": rel,
                "label": label or os.path.basename(rel),
                "source": "manual",
            }
        )
        v["pins"] = pins
        save_store(store)
        return get_vault(vid) or v
    raise ValueError("Vault nicht gefunden")


def remove_pin(vid: str, rel_path: str) -> dict:
    rel = (rel_path or "").strip().replace("\\", "/").lstrip("/")
    store = load_store(force=True)
    for v in store.get("vaults") or []:
        if v.get("id") != vid:
            continue
        v["pins"] = [p for p in (v.get("pins") or []) if p.get("path") != rel]
        save_store(store)
        return get_vault(vid) or v
    raise ValueError("Vault nicht gefunden")


def public_snapshot() -> dict:
    """API/UI."""
    store = load_store(force=True)
    vaults = []
    for v in store.get("vaults") or []:
        vv = dict(v)
        vv["exists"] = os.path.isdir(v["path"])
        vv["obsidian_uri"] = "obsidian://open?vault=" + quote(v["name"])
        vaults.append(vv)
    return {
        "ok": True,
        "store_path": USER_STORE,
        "defaults_path": DEFAULTS_STORE,
        "vaults": vaults,
        "agent_paths": paths_for_agent(),
        "private_paths": private_paths(),
    }
