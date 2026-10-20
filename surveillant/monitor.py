#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Surveillance des sites du Groupe APMvie.

- Aucune dépendance externe : uniquement la bibliothèque standard de Python (3.9 ou plus).
- Lit la liste des sites dans sites.json et contrôle chacun d'eux.
- Prévient par e-mail, notification push (ntfy) et SMS (Free Mobile) quand quelque chose ne va pas.
- Mémorise l'état dans state.json et l'historique des pannes dans incidents.jsonl.

Utilisation :
    python3 monitor.py                 contrôle normal (c'est ce que fait GitHub toutes les 5 minutes)
    python3 monitor.py --dry-run       contrôle sans rien envoyer ni enregistrer
    python3 monitor.py --test-alerte   envoie une alerte de test sur tous les canaux configurés
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

DOSSIER = Path(__file__).resolve().parent
sys.path.insert(0, str(DOSSIER.parent))

from commun.alertes import (  # noqa: E402
    Notifier, construire_email, envoyer_email, envoyer_push, envoyer_sms, sans_accents, url_sms,
)

USER_AGENT = "SurveillanceGroupeAPMvie/1.0 (controle de disponibilite)"

PARAMETRES_PAR_DEFAUT = {
    "timeout_s": 15,             # attente maximale d'une réponse
    "pause_nouvel_essai_s": 10,  # pause avant le 2e essai en cas d'échec
    "seuil_echecs": 2,           # nombre de contrôles ratés d'affilée avant l'alerte
    "ssl_alerte_jours": [14, 7, 3],
    "rappel_apres_min": 60,      # 1er rappel si la panne dure
    "rappel_toutes_les_h": 6,    # puis un rappel toutes les 6 h
    "lenteur_ms": 5000,          # au-dessus, la page est signalée « lente » dans le journal
    "fuseau": "Europe/Paris",
}

# Pour chaque type de problème : (description lisible, piste la plus probable)
CAUSES = {
    "dns": (
        "Le nom de domaine ne répond pas (problème DNS)",
        "Le domaine a peut-être expiré ou sa zone DNS a été modifiée. Vérifier dans l'espace client OVH "
        "que le domaine est actif et que les enregistrements DNS n'ont pas changé.",
    ),
    "timeout": (
        "Le site ne répond pas à temps",
        "Serveur surchargé ou en panne chez l'hébergeur. Réessayer dans quelques minutes ; si ça persiste, "
        "regarder l'état de l'hébergement ou contacter son support.",
    ),
    "connexion": (
        "Connexion refusée ou coupée",
        "Le serveur est injoignable ou arrêté. Vérifier l'état de l'hébergement chez le fournisseur.",
    ),
    "ssl_invalide": (
        "Le certificat de sécurité (HTTPS) est invalide ou expiré",
        "Le cadenas HTTPS ne peut plus être validé. Vérifier le certificat chez l'hébergeur (OVH ou "
        "Systeme.io) et le renouveler ou le réactiver.",
    ),
    "ssl": (
        "Erreur de connexion sécurisée (HTTPS)",
        "La connexion sécurisée échoue. Souvent une mauvaise configuration du certificat ou du domaine "
        "chez l'hébergeur.",
    ),
    "http_5xx": (
        "Le serveur répond mais plante (erreur 5xx)",
        "Souvent un bug dans le code ou un service en panne. Se demander : qu'est-ce qui a été publié "
        "ou modifié juste avant ? Annuler ou corriger cette modification.",
    ),
    "http_404": (
        "Page introuvable (erreur 404)",
        "La page a été supprimée, déplacée ou l'adresse a changé. Vérifier ce qui est publié à cette adresse.",
    ),
    "http_4xx": (
        "Accès refusé ou requête rejetée (erreur 4xx)",
        "La page existe mais refuse l'accès. Vérifier les protections, mots de passe ou restrictions "
        "récemment activés.",
    ),
    "contenu": (
        "La page s'affiche mais le contenu attendu est absent",
        "Page vide, remplacée par une page d'erreur de l'hébergeur, ou contenu modifié. Ouvrir la page "
        "et la comparer à la version normale.",
    ),
    "redirection": (
        "La redirection ne mène pas à la bonne adresse",
        "Vérifier la redirection configurée chez OVH pour ce domaine (elle doit mener à l'adresse attendue).",
    ),
    "boucle_redirection": (
        "Boucle de redirections (le site tourne en rond)",
        "Deux redirections se renvoient l'une à l'autre. Vérifier les redirections du domaine et du site.",
    ),
    "inconnue": (
        "Problème non identifié",
        "Voir le détail technique ci-dessous et le transmettre à Claude.",
    ),
}

QUI_REPARE = {
    "claude": (
        "Ce site est du code créé avec Claude : transmets le message ci-dessous à Claude, "
        "il diagnostique et corrige. Tu valides avant toute publication."
    ),
    "plateforme": (
        "Cette page est hébergée sur une plateforme tierce ({hebergeur}). Claude peut t'aider à "
        "diagnostiquer, mais la correction se fait dans l'éditeur de la plateforme. Si c'est une panne de "
        "leurs serveurs, il faut attendre leur rétablissement."
    ),
    "domaine": (
        "Redirection de nom de domaine (OVH) : transmets le message ci-dessous à Claude, "
        "il te guide pas à pas dans l'espace client OVH."
    ),
    None: "Transmets le message ci-dessous à Claude, il t'aide à diagnostiquer et à corriger.",
}


# --------------------------------------------------------------------------------------
# Outils : dates, durées
# --------------------------------------------------------------------------------------

def maintenant_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(texte: str) -> datetime:
    return datetime.strptime(texte, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fuseau(params):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(params.get("fuseau", "Europe/Paris"))
        except Exception:
            pass
    return timezone.utc


def fmt_local(dt: datetime, params) -> str:
    return dt.astimezone(fuseau(params)).strftime("%d/%m/%Y %H:%M")


def fmt_heure(dt: datetime, params) -> str:
    return dt.astimezone(fuseau(params)).strftime("%H:%M")


def duree_lisible(secondes: float) -> str:
    secondes = int(max(0, secondes))
    if secondes < 60:
        return f"{secondes} s"
    minutes = round(secondes / 60)
    if minutes < 60:
        return f"{minutes} min"
    heures, minutes = divmod(minutes, 60)
    if heures < 24:
        return f"{heures} h {minutes:02d}"
    jours, heures = divmod(heures, 24)
    return f"{jours} j {heures} h"


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

def charger_config(chemin) -> dict:
    try:
        with open(chemin, encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"Fichier de configuration introuvable : {chemin}")
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"sites.json est mal écrit (ligne {exc.lineno}, colonne {exc.colno}) : {exc.msg}. "
            "Une virgule manquante ou en trop est la cause la plus fréquente."
        )
    sites = config.get("sites")
    if not isinstance(sites, list):
        raise SystemExit("sites.json doit contenir une liste « sites ».")
    deja_vus = set()
    for site in sites:
        for champ in ("id", "url"):
            if not site.get(champ):
                raise SystemExit(f"Un site de sites.json n'a pas de champ « {champ} » : {site}")
        if site["id"] in deja_vus:
            raise SystemExit(f"L'identifiant « {site['id']} » apparaît deux fois dans sites.json.")
        deja_vus.add(site["id"])
        if not str(site["url"]).startswith(("http://", "https://")):
            raise SystemExit(f"L'adresse du site « {site['id']} » doit commencer par http:// ou https://")
    return config


def parametres(config) -> dict:
    return {**PARAMETRES_PAR_DEFAUT, **config.get("parametres", {})}


# --------------------------------------------------------------------------------------
# Contrôle d'un site
# --------------------------------------------------------------------------------------

class TropDeRedirections(Exception):
    pass


def _expiration_certificat(sock):
    try:
        cert = sock.getpeercert()
        if cert and "notAfter" in cert:
            return ssl.cert_time_to_seconds(cert["notAfter"])
    except Exception:
        pass
    return None


def charger_page(url, timeout, ssl_context=None, max_redirections=5):
    """Télécharge une page en suivant les redirections une par une."""
    debut = time.monotonic()
    chaine = []
    courant = url
    cert_expire = None
    for rang in range(max_redirections + 1):
        parts = urllib.parse.urlsplit(courant)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"adresse invalide : {courant}")
        chemin = parts.path or "/"
        if parts.query:
            chemin += "?" + parts.query
        if parts.scheme == "https":
            conn = http.client.HTTPSConnection(
                parts.hostname, parts.port, timeout=timeout,
                context=ssl_context or ssl.create_default_context(),
            )
        else:
            conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
        try:
            conn.connect()
            if rang == 0 and parts.scheme == "https":
                cert_expire = _expiration_certificat(conn.sock)
            conn.request("GET", chemin, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9",
                "Connection": "close",
            })
            reponse = conn.getresponse()
            statut = reponse.status
            location = reponse.getheader("Location")
            protection = (reponse.getheader("cf-mitigated") or "").strip().lower()
            corps = b""
            if not (statut in (301, 302, 303, 307, 308) and location):
                corps = reponse.read(300_000)
        finally:
            conn.close()
        chaine.append((courant, statut))
        if statut in (301, 302, 303, 307, 308) and location:
            courant = urllib.parse.urljoin(courant, location)
            continue
        return {
            "statut": statut,
            "url_finale": courant,
            "corps": corps.decode("utf-8", "replace"),
            "ms": int((time.monotonic() - debut) * 1000),
            "cert_expire": cert_expire,
            "chaine": chaine,
            "protection": protection,
        }
    raise TropDeRedirections(f"plus de {max_redirections} redirections successives depuis {url}")


def classer_exception(exc):
    """Transforme une erreur technique en (type de problème, détail)."""
    if isinstance(exc, TropDeRedirections):
        return "boucle_redirection", str(exc)
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "ssl_invalide", getattr(exc, "verify_message", None) or str(exc)
    if isinstance(exc, ssl.SSLError):
        return "ssl", str(exc)
    if isinstance(exc, socket.gaierror):
        return "dns", str(exc)
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout", "aucune réponse dans le délai imparti"
    if isinstance(exc, (ConnectionError, http.client.HTTPException, OSError)):
        return "connexion", f"{type(exc).__name__} : {exc}"
    return "inconnue", f"{type(exc).__name__} : {exc}"


def meme_destination(url_finale, attendue) -> bool:
    a = urllib.parse.urlsplit(url_finale)
    b = urllib.parse.urlsplit(attendue)
    if a.scheme != b.scheme or a.netloc.lower() != b.netloc.lower():
        return False
    return a.path.rstrip("/").startswith(b.path.rstrip("/"))


PROTEGE = ("protégé par une vérification anti-robots (Cloudflare) : le site répond, mais son contenu ne peut pas être "
           "vérifié automatiquement")


def evaluer_page(site, page):
    """Renvoie (None, "") si la page est bonne, sinon (type de problème, détail)."""
    statut = page["statut"]
    if statut in (403, 429, 503) and page.get("protection") == "challenge":
        # Cloudflare demande au robot de prouver qu'il est humain : le site répond, mais on ne voit pas la page
        return None, PROTEGE
    if statut >= 500:
        return "http_5xx", f"code HTTP {statut}"
    if statut == 404:
        return "http_404", "code HTTP 404"
    if statut >= 400:
        return "http_4xx", f"code HTTP {statut}"
    if not 200 <= statut < 300:
        return "redirection", f"réponse inattendue (code HTTP {statut}) à {page['url_finale']}"
    attendue = site.get("redirige_vers")
    if attendue and not meme_destination(page["url_finale"], attendue):
        return "redirection", f"mène à {page['url_finale']} au lieu de {attendue}"
    mots = site.get("mot_cle")
    if mots:
        if isinstance(mots, str):
            mots = [mots]
        texte = page["corps"].lower()
        manquants = [m for m in mots if m.lower() not in texte]
        if manquants:
            return "contenu", "texte introuvable sur la page : " + ", ".join(f"« {m} »" for m in manquants)
    return None, ""


def controler_site(site, params, ssl_context=None, pause=time.sleep):
    """Contrôle un site (2 essais maximum) et renvoie un résumé."""
    res = {}
    for essai in (1, 2):
        res = {"id": site["id"], "ok": False, "cause": None, "detail": "", "statut": None,
               "ms": None, "ssl_jours": None, "url_finale": None}
        try:
            page = charger_page(site["url"], params["timeout_s"], ssl_context)
            res["statut"] = page["statut"]
            res["ms"] = page["ms"]
            res["url_finale"] = page["url_finale"]
            if page["cert_expire"] is not None:
                res["ssl_jours"] = (page["cert_expire"] - time.time()) / 86400
            cause, detail = evaluer_page(site, page)
            res["ok"] = cause is None
            if res["ok"] and detail:
                res["note"] = detail
            res["cause"], res["detail"] = cause, detail
        except Exception as exc:  # noqa: BLE001 - toute erreur doit être classée, jamais plantée
            res["cause"], res["detail"] = classer_exception(exc)
        if res["ok"] or essai == 2:
            break
        pause(params["pause_nouvel_essai_s"])
    return res


# --------------------------------------------------------------------------------------
# Messages d'alerte
# --------------------------------------------------------------------------------------

def construire_message(genre, site, res, e, maintenant, params, jours_ssl=None):
    """Prépare le texte d'une alerte. genre : panne, rappel, retour, ssl ou test."""
    nom = site.get("nom", site["id"])
    entreprise = site.get("entreprise", "")
    url = site["url"]
    hebergeur = site.get("hebergeur", "non précisé")
    critique = bool(site.get("critique"))
    cause_cle = e.get("cause") or (res or {}).get("cause") or "inconnue"
    cause_txt, piste = CAUSES.get(cause_cle, CAUSES["inconnue"])
    detail = e.get("detail") or (res or {}).get("detail") or ""
    debut = parse_iso(e["premier_echec"]) if e.get("premier_echec") else maintenant
    depuis = fmt_local(debut, params)
    seuil = site.get("seuil_echecs", params["seuil_echecs"])
    qui = QUI_REPARE.get(site.get("reparation"), QUI_REPARE[None]).format(hebergeur=hebergeur)
    importance = "critique" if critique else "normale"
    etiquette = f"[{entreprise}] " if entreprise else ""
    note = site.get("note")

    fiche = [
        f"Site : {nom}" + (f" ({entreprise})" if entreprise else ""),
        f"Adresse : {url}",
        f"Importance : {importance}",
        f"Hébergé chez : {hebergeur}",
    ]
    if note:
        fiche.append(f"Note : {note}")

    msg = {"priorite": 3, "tags": [], "sms": None}

    if genre == "panne":
        msg["sujet"] = f"🔴 {etiquette}ALERTE : {nom} est en panne"
        msg["corps"] = "\n".join([
            f"{nom} ne répond plus correctement.",
            "",
            *fiche,
            "",
            f"Problème : {cause_txt}",
            f"Détail technique : {detail}",
            f"Depuis : {depuis} (heure de Paris), constaté sur {seuil} contrôles consécutifs.",
            "",
            f"Qui répare : {qui}",
            f"Piste la plus probable : {piste}",
            "",
            "---- Message à copier-coller à Claude ----",
            f"Alerte de surveillance : « {nom} » ({url}) est en panne depuis {depuis}. "
            f"Problème détecté : {cause_txt}. Détail technique : {detail}. Hébergé chez {hebergeur}. "
            "Aide-moi à trouver la cause et à corriger. Je valide avant toute publication.",
        ])
        msg["push_titre"] = f"ALERTE - {nom} en panne"
        msg["push_texte"] = f"{cause_txt}. Depuis {fmt_heure(debut, params)}. Détails par e-mail."
        msg["priorite"] = 5 if critique else 4
        msg["tags"] = ["rotating_light"]
        msg["sms"] = sans_accents(
            f"ALERTE {entreprise or 'SITE'}: {nom} en panne ({cause_txt}) depuis {fmt_heure(debut, params)}. "
            "Details par e-mail."
        )[:300]

    elif genre == "rappel":
        duree = duree_lisible((maintenant - debut).total_seconds())
        msg["sujet"] = f"🔴 {etiquette}TOUJOURS EN PANNE : {nom} (depuis {duree})"
        msg["corps"] = "\n".join([
            f"{nom} est toujours en panne depuis {depuis} ({duree}).",
            "",
            *fiche,
            "",
            f"Problème : {cause_txt}",
            f"Détail technique : {detail}",
            "",
            f"Qui répare : {qui}",
            f"Piste la plus probable : {piste}",
        ])
        msg["push_titre"] = f"Toujours en panne - {nom}"
        msg["push_texte"] = f"Panne en cours depuis {duree}. {cause_txt}."
        msg["priorite"] = 4
        msg["tags"] = ["rotating_light"]

    elif genre == "retour":
        duree = duree_lisible((maintenant - debut).total_seconds())
        msg["sujet"] = f"🟢 {etiquette}RÉTABLI : {nom} fonctionne de nouveau"
        msg["corps"] = "\n".join([
            f"{nom} est de nouveau accessible.",
            "",
            *fiche,
            "",
            f"Durée de la panne : environ {duree} (du {depuis} jusqu'à {fmt_local(maintenant, params)}).",
            f"Cause constatée : {cause_txt}",
            "",
            "Si la panne s'est résolue toute seule, garde-la en tête : si elle se répète, "
            "dis-le à Claude pour qu'il cherche la cause de fond.",
        ])
        msg["push_titre"] = f"Rétabli - {nom}"
        msg["push_texte"] = f"De nouveau accessible après environ {duree}."
        msg["priorite"] = 3
        msg["tags"] = ["white_check_mark"]
        msg["sms"] = sans_accents(f"{entreprise or 'SITE'}: {nom} fonctionne de nouveau (panne d'environ {duree}).")[:300]

    elif genre == "ssl":
        jours = max(0, int(jours_ssl if jours_ssl is not None else 0))
        msg["sujet"] = f"🟠 {etiquette}CERTIFICAT : le HTTPS de {nom} expire dans {jours} jours"
        msg["corps"] = "\n".join([
            f"Le certificat de sécurité (cadenas HTTPS) de {nom} expire dans {jours} jours.",
            "",
            *fiche,
            "",
            "Normalement il se renouvelle tout seul : si cette alerte arrive, le renouvellement automatique "
            "ne fonctionne probablement pas. Sans action, le site affichera un avertissement de sécurité "
            "aux visiteurs à l'expiration.",
            "",
            "---- Message à copier-coller à Claude ----",
            f"Le certificat HTTPS de {url} expire dans {jours} jours (hébergé chez {hebergeur}). "
            "Aide-moi à vérifier et à forcer le renouvellement.",
        ])
        msg["push_titre"] = f"Certificat - {nom}"
        msg["push_texte"] = f"Le HTTPS expire dans {jours} jours. Détails par e-mail."
        msg["priorite"] = 4 if jours <= 3 else 3
        msg["tags"] = ["warning"]

    elif genre == "test":
        msg["sujet"] = "🧪 Test d'alerte : le système de surveillance fonctionne"
        msg["corps"] = (
            "Ceci est un message de test du système de surveillance du Groupe APMvie.\n"
            "Si tu lis ce message, ce canal d'alerte fonctionne."
        )
        msg["push_titre"] = "Test - surveillance APMvie"
        msg["push_texte"] = "Ceci est un test. Ce canal d'alerte fonctionne."
        msg["priorite"] = 3
        msg["tags"] = ["test_tube"]
        msg["sms"] = "Test surveillance APMvie : ce canal d'alerte fonctionne."
    else:
        raise ValueError(f"genre d'alerte inconnu : {genre}")
    return msg


# Envoi des alertes : voir commun/alertes.py (partagé par tous les agents)


# --------------------------------------------------------------------------------------
# Machine d'états : quand alerter, quand rappeler, quand clore
# --------------------------------------------------------------------------------------

CLES_PANNE = ("echecs", "premier_echec", "alerte_envoyee", "cause", "detail", "dernier_rappel")


def rappel_du(e, maintenant, params) -> bool:
    reference = e.get("dernier_rappel")
    if reference:
        return maintenant - parse_iso(reference) >= timedelta(hours=params["rappel_toutes_les_h"])
    return maintenant - parse_iso(e["premier_echec"]) >= timedelta(minutes=params["rappel_apres_min"])


def traiter_resultat(site, res, etat_sites, maintenant, params, envoyer):
    """Met à jour l'état d'un site. Renvoie la liste des pannes terminées à enregistrer."""
    sid = site["id"]
    e = etat_sites.get(sid, {})
    seuil = site.get("seuil_echecs", params["seuil_echecs"])
    incidents = []

    if not res["ok"]:
        premiere_fois = e.get("echecs", 0) == 0
        e["echecs"] = min(e.get("echecs", 0) + 1, seuil)  # plafonné : l'état ne change plus pendant une panne
        if premiere_fois:
            e["premier_echec"] = iso(maintenant)
            e["cause"] = res["cause"]
            e["detail"] = res["detail"]
        elif e.get("cause") != res["cause"]:
            e["cause"] = res["cause"]
            e["detail"] = res["detail"]
        if e["echecs"] >= seuil:
            if not e.get("alerte_envoyee"):
                e["alerte_envoyee"] = bool(envoyer("panne", site, res, e, maintenant))
            elif rappel_du(e, maintenant, params):
                if envoyer("rappel", site, res, e, maintenant):
                    e["dernier_rappel"] = iso(maintenant)
    else:
        if e.get("echecs", 0) >= seuil:
            debut = parse_iso(e["premier_echec"])
            incidents.append({
                "site": sid,
                "nom": site.get("nom", sid),
                "debut": iso(debut),
                "fin": iso(maintenant),
                "duree_s": int((maintenant - debut).total_seconds()),
                "cause": e.get("cause"),
            })
            if e.get("alerte_envoyee"):
                envoyer("retour", site, res, e, maintenant)
        for cle in CLES_PANNE:
            e.pop(cle, None)

    # Certificat HTTPS : alerte à 14, 7 puis 3 jours avant l'expiration
    jours = res.get("ssl_jours")
    if jours is not None:
        niveau = None
        for seuil_jours in sorted(params["ssl_alerte_jours"]):
            if jours <= seuil_jours:
                niveau = seuil_jours
                break
        precedent = e.get("ssl_niveau")
        if niveau is None:
            e.pop("ssl_niveau", None)
        elif precedent is None or niveau < precedent:
            if envoyer("ssl", site, res, e, maintenant):
                e["ssl_niveau"] = niveau

    if e:
        etat_sites[sid] = e
    else:
        etat_sites.pop(sid, None)
    return incidents


def appliquer(sites, resultats, etat, params, envoyer, maintenant):
    """Applique les résultats de tous les sites. Renvoie les pannes terminées."""
    etat_sites = etat.setdefault("sites", {})
    actifs = {s["id"] for s in sites}
    for ancien in list(etat_sites):
        if ancien not in actifs:
            del etat_sites[ancien]
    incidents = []
    for site in sites:
        incidents += traiter_resultat(site, resultats[site["id"]], etat_sites, maintenant, params, envoyer)
    return incidents


# --------------------------------------------------------------------------------------
# Fichiers d'état
# --------------------------------------------------------------------------------------

def lire_etat(chemin: Path) -> dict:
    if chemin.exists():
        try:
            etat = json.loads(chemin.read_text(encoding="utf-8"))
            if isinstance(etat, dict):
                return etat
        except json.JSONDecodeError:
            print("state.json illisible : on repart d'un état vide.")
    return {"sites": {}}


def ecrire_si_change(chemin: Path, contenu: str):
    ancien = chemin.read_text(encoding="utf-8") if chemin.exists() else None
    if ancien != contenu:
        chemin.write_text(contenu, encoding="utf-8")


def serialiser_etat(etat: dict) -> str:
    return json.dumps(etat, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def ligne_resume(site, res, params) -> str:
    if res["ok"]:
        extra = f"{res['ms']} ms"
        if res.get("ssl_jours") is not None:
            extra += f", certificat valide encore {int(res['ssl_jours'])} j"
        if res["ms"] is not None and res["ms"] > params["lenteur_ms"]:
            extra += "  (LENT)"
        if res.get("note"):
            extra += f"  ({res['note']})"
        return f"OK     {site['id']:<26} {extra}"
    return f"ECHEC  {site['id']:<26} {res['cause']} - {res['detail']}"


# --------------------------------------------------------------------------------------
# Programme principal
# --------------------------------------------------------------------------------------

def main(argv=None, env=None) -> int:
    parser = argparse.ArgumentParser(description="Surveillance des sites du Groupe APMvie")
    parser.add_argument("--config", default=str(DOSSIER / "sites.json"))
    parser.add_argument("--etat", default=str(DOSSIER / "state.json"))
    parser.add_argument("--incidents", default=str(DOSSIER / "incidents.jsonl"))
    parser.add_argument("--dry-run", action="store_true", help="ne rien envoyer, ne rien enregistrer")
    parser.add_argument("--test-alerte", action="store_true", help="envoyer une alerte de test")
    args = parser.parse_args(argv)

    config = charger_config(args.config)
    params = parametres(config)
    notifier = Notifier(env=env, dry_run=args.dry_run)
    maintenant = maintenant_utc()

    if args.test_alerte:
        canaux = notifier.canaux()
        print("Canaux configurés :", ", ".join(canaux) if canaux else "aucun")
        site_test = {"id": "test", "nom": "Site de test", "entreprise": "Groupe APMvie",
                     "url": "https://exemple.invalid", "critique": True}
        message = construire_message("test", site_test, None, {}, maintenant, params)
        return 0 if notifier.envoyer(message, avec_sms=True) else 2

    sites = [s for s in config["sites"] if s.get("actif", True)]
    inactifs = len(config["sites"]) - len(sites)
    if not sites:
        print(f"Aucun site actif à surveiller ({inactifs} site(s) inactif(s) dans sites.json).")
        return 0

    with ThreadPoolExecutor(max_workers=min(8, len(sites))) as pool:
        liste = list(pool.map(lambda s: controler_site(s, params), sites))
    resultats = {s["id"]: r for s, r in zip(sites, liste)}
    for s in sites:
        print(ligne_resume(s, resultats[s["id"]], params))
    if inactifs:
        print(f"({inactifs} site(s) inactif(s), non surveillé(s))")

    chemin_etat = Path(args.etat)
    etat = lire_etat(chemin_etat)
    alertes_perdues = []

    def envoyer(genre, site, res, e, quand):
        message = construire_message(genre, site, res, e, quand, params, res.get("ssl_jours") if res else None)
        avec_sms = bool(site.get("critique")) and genre in ("panne", "retour")
        ok = notifier.envoyer(message, avec_sms=avec_sms)
        if not ok:
            alertes_perdues.append((genre, site["id"]))
        return ok

    incidents = appliquer(sites, resultats, etat, params, envoyer, maintenant)

    if not args.dry_run:
        ecrire_si_change(chemin_etat, serialiser_etat(etat))
        if incidents:
            with open(args.incidents, "a", encoding="utf-8") as f:
                for incident in incidents:
                    f.write(json.dumps(incident, ensure_ascii=False, sort_keys=True) + "\n")

    en_panne = [sid for sid, e in etat.get("sites", {}).items() if e.get("alerte_envoyee")]
    if en_panne:
        print("Pannes en cours :", ", ".join(en_panne))
    if alertes_perdues:
        print(f"ATTENTION : {len(alertes_perdues)} alerte(s) n'ont pas pu être envoyées ; nouvel essai au prochain contrôle.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
