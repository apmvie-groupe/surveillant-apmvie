# -*- coding: utf-8 -*-
"""
Les données publiques officielles, gratuites et sans clé :

  domaine_expiration   date d'expiration d'un nom de domaine (registre RDAP)
  dns                  les réglages d'un domaine (DNS), lus par un service DNS public (Google)
  entreprise           l'état d'une société dans l'annuaire officiel (recherche-entreprises.api.gouv.fr)
  bodacc               les annonces légales publiées au BODACC sur un numéro SIREN
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

UA = {"User-Agent": "Agents-APMvie", "Accept": "application/json, application/rdap+json"}


def _lire(url, lire=None):
    if lire:
        return lire(url)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


# --- Noms de domaine ---------------------------------------------------------------------

def domaine_expiration(domaine: str, lire=None) -> str | None:
    """Date (AAAA-MM-JJ) d'expiration du domaine, lue dans le registre officiel (RDAP)."""
    d = _lire(f"https://rdap.org/domain/{urllib.parse.quote(domaine)}", lire)
    for e in d.get("events", []):
        if e.get("eventAction") in ("expiration", "registration expiration"):
            return (e.get("eventDate") or "")[:10] or None
    return None


# --- DNS ---------------------------------------------------------------------------------

TYPES = {"A": 1, "NS": 2, "CNAME": 5, "MX": 15, "TXT": 16, "AAAA": 28}


def dns(nom: str, genre: str = "TXT", lire=None) -> list:
    """Les valeurs d'un enregistrement DNS (liste triée de textes)."""
    d = _lire(f"https://dns.google/resolve?name={urllib.parse.quote(nom)}&type={genre}", lire)
    valeurs = [r.get("data", "").strip('"').replace('" "', "") for r in d.get("Answer", []) or []
               if r.get("type") == TYPES.get(genre)]
    return sorted(valeurs)


def controle_email_domaine(domaine: str, selecteurs_dkim=(), lire=None) -> list:
    """[(élément, ok, explication)] pour SPF, DKIM et DMARC."""
    sortie = []
    txt = dns(domaine, "TXT", lire)
    spf = [t for t in txt if t.lower().startswith("v=spf1")]
    if len(spf) == 1:
        sortie.append(("SPF", True, "présent"))
    elif not spf:
        sortie.append(("SPF", False, "absent : tes e-mails risquent d'arriver dans les indésirables"))
    else:
        sortie.append(("SPF", False, f"{len(spf)} enregistrements SPF : il n'en faut qu'un"))
    dmarc = [t for t in dns(f"_dmarc.{domaine}", "TXT", lire) if t.lower().startswith("v=dmarc1")]
    sortie.append(("DMARC", bool(dmarc), "présent" if dmarc else "absent : Gmail et Yahoo l'exigent pour les envois en nombre"))
    for s in selecteurs_dkim:
        cle = [t for t in dns(f"{s}._domainkey.{domaine}", "TXT", lire) if "p=" in t]
        sortie.append((f"DKIM ({s})", bool(cle), "présent" if cle else "absent : signature des e-mails manquante"))
    return sortie


def empreinte_dns(domaine: str, lire=None) -> dict:
    """Les réglages principaux d'un domaine, pour repérer un changement suspect."""
    return {g: dns(domaine, g, lire) for g in ("A", "MX", "NS")}


# --- Entreprises -------------------------------------------------------------------------

def entreprise(siren: str, lire=None) -> dict | None:
    """{nom, etat, date_creation, dirigeants, siege} depuis l'annuaire officiel des entreprises."""
    d = _lire(f"https://recherche-entreprises.api.gouv.fr/search?q={urllib.parse.quote(siren)}", lire)
    for r in d.get("results", []):
        if r.get("siren") == siren:
            return {"nom": r.get("nom_complet"), "etat": r.get("etat_administratif"),
                    "date_creation": r.get("date_creation"),
                    "dirigeants": [" ".join(x for x in (g.get("prenoms"), g.get("nom")) if x) or g.get("denomination")
                                   for g in r.get("dirigeants", [])],
                    "siege": (r.get("siege") or {}).get("adresse")}
    return None


def bodacc(siren: str, lire=None, nombre=20) -> list:
    """Les dernières annonces BODACC sur ce SIREN : [{date, famille, contenu}]."""
    ou = urllib.parse.quote(f'registre like "{siren}"')
    d = _lire("https://bodacc-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/annonces-commerciales/records"
              f"?where={ou}&order_by=dateparution%20desc&limit={nombre}", lire)
    return [{"date": r.get("dateparution"), "famille": r.get("familleavis_lib"), "id": r.get("id"),
             "contenu": (r.get("commercant") or "") + (" — " + r.get("tribunal") if r.get("tribunal") else "")}
            for r in d.get("results", [])]
