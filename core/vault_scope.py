# -*- coding: utf-8 -*-
"""Welcher Vault in welchem Turn: memory-wiki immer, Arbeits-Vault nur mit Apfel."""
from __future__ import annotations

import os

WIKI_NAME = "memory-wiki"


def is_wiki_root(path) -> bool:
    if not path:
        return False
    base = os.path.basename(os.path.normpath(str(path))).lower()
    if base == WIKI_NAME:
        return True
    p = str(path).replace("\\", "/").lower()
    return f"/{WIKI_NAME}" in f"/{p.strip('/')}" or p.rstrip("/").endswith(WIKI_NAME)


def wiki_roots():
    """Aktive memory-wiki-Pfade aus config.VAULT_PATHS (Registry-Fallback)."""
    from . import config

    paths = list(getattr(config, "VAULT_PATHS", None) or [])
    if not paths:
        try:
            from . import vaults_registry as _vr

            paths = list(_vr.paths_for_agent() or [])
        except Exception:
            paths = []
    out = []
    seen = set()
    for raw in paths:
        if not raw or not is_wiki_root(raw):
            continue
        try:
            real = os.path.realpath(raw)
        except OSError:
            real = str(raw)
        if real in seen:
            continue
        seen.add(real)
        out.append(raw)
    return out


def path_in_roots(path, roots) -> bool:
    """Index-Pfad (/VaultName/rel) oder Abs-Pfad gegen Vault-Roots."""
    if not path or not roots:
        return False
    names = set()
    reals = []
    for raw in roots:
        if not raw:
            continue
        names.add(os.path.basename(os.path.normpath(str(raw))).lower())
        try:
            reals.append(os.path.realpath(raw))
        except OSError:
            reals.append(str(raw))
    p = str(path).replace("\\", "/")
    pl = p.lower().lstrip("/")
    first = pl.split("/", 1)[0]
    if first in names:
        return True
    try:
        if os.path.isabs(p):
            real = os.path.realpath(p)
            for rr in reals:
                marker = rr.rstrip("/\\")
                if real == marker or real.startswith(marker + os.sep) or real.startswith(marker + "/"):
                    return True
    except OSError:
        pass
    return False


def private_vault_names():
    """Basenamen der Privat-Vaults (Registry mode=private), kleingeschrieben."""
    try:
        from . import vaults_registry as _vr

        return {
            os.path.basename(os.path.normpath(str(p))).lower()
            for p in _vr.private_paths()
            if p
        }
    except Exception:
        return set()


def is_private_hit(path, names=None) -> bool:
    """Index-Treffer (/VaultName/rel oder Abs-Pfad) aus einem Privat-Vault?

    Der Embedding-/PDF-Index kann Dokumente von Vaults enthalten, die erst
    später auf privat gestellt wurden — deshalb Filter bei der Abfrage, nicht
    nur beim Index-Aufbau. Abs-Pfade entscheidet der spezifischste Root
    (Home ist privat, darunter liegende rw-Vaults nicht).
    """
    if not path:
        return False
    if names is None:
        names = private_vault_names()
    p = str(path).replace("\\", "/")
    first = p.lstrip("/").split("/", 1)[0].lower()
    if first in names:
        return True
    try:
        if os.path.isabs(p) and os.path.exists(p):
            from . import vaults_registry as _vr

            return bool(_vr.is_private_path(p))
    except Exception:
        pass
    return False
