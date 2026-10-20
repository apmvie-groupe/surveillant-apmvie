# -*- coding: utf-8 -*-
"""
Le « mode transmission », déclenché par Le Passeur quand Enzo ne donne plus de nouvelles.

Pendant ce mode :
  - les alertes de tous les agents partent aussi à ses filles (en plus de lui), avec un bandeau d'explication ;
  - les agents de confort (Rédacteur, Régisseur, Veilleur) se taisent ;
  - dès qu'Enzo donne signe de vie, Le Passeur arrête le mode et tout redevient normal.

L'état du mode est dans passeur/transmission.json (écrit par Le Passeur). Les adresses des filles ne sont jamais
écrites dans un fichier : elles sont dans le secret GitHub PASSEUR_CONTACTS.
"""

from __future__ import annotations

import json
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
FICHIER = RACINE / "passeur" / "transmission.json"
REGLAGES = RACINE / "passeur" / "passeur.json"


def _lire(chemin: Path) -> dict:
    try:
        return json.loads(Path(chemin).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def etat(env=None) -> dict | None:
    """Le mode transmission s'il est actif, sinon None."""
    env = env or {}
    if str(env.get("PASSEUR_SANS_TRANSMISSION", "")).strip():
        return None
    t = _lire(Path(env.get("PASSEUR_TRANSMISSION") or FICHIER))
    return t if t.get("actif") else None


def contacts(env) -> list:
    """[{nom, email, ...}] depuis le secret PASSEUR_CONTACTS (vide si absent ou mal écrit)."""
    try:
        liste = json.loads((env or {}).get("PASSEUR_CONTACTS") or "[]")
        return [c for c in liste if isinstance(c, dict) and c.get("email")]
    except json.JSONDecodeError:
        return []


def silence(agent_id: str, env=None) -> bool:
    """Vrai si cet agent doit se taire pendant le mode transmission."""
    if not etat(env):
        return False
    reglages = _lire(Path((env or {}).get("PASSEUR_REGLAGES") or REGLAGES))
    return agent_id in reglages.get("agents_silencieux", [])


def habiller(msg: dict, t: dict, noms: str) -> dict:
    """Ajoute le bandeau d'explication en tête du message."""
    bandeau = (f"ℹ️ MODE TRANSMISSION — Enzo ne donne plus de nouvelles depuis le {t.get('depuis_texte', '?')}. "
               f"Ce message vous est envoyé, {noms}, en plus de lui. Le document de transmission explique quoi faire ; "
               "si Enzo réapparaît, tout redevient normal et vous ne recevrez plus ces messages.\n\n")
    return dict(msg, sujet="[Transmission] " + msg["sujet"], corps=bandeau + msg["corps"])
