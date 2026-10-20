# -*- coding: utf-8 -*-
"""
Boîte à outils d'alertes partagée par tous les agents du Groupe APMvie.

Un message est un dictionnaire :
    sujet, corps            -> e-mail
    push_titre, push_texte  -> notification sur le téléphone (appli ntfy)
    priorite (1 à 5), tags  -> importance et icône de la notification
    sms (facultatif)        -> texte court, sans accents, envoyé par Free Mobile
    clic (facultatif)       -> adresse ouverte quand on touche la notification

Les secrets (mots de passe, clés) sont lus dans les variables d'environnement, jamais dans les fichiers.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import unicodedata
import urllib.parse
import urllib.request
from email.message import EmailMessage

from commun import transmission

USER_AGENT = "AgentsGroupeAPMvie/1.0"


def sans_accents(texte: str) -> str:
    return unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode("ascii")


ENTETE_AGENTS = "X-Agents-APMvie"


def construire_email(env, sujet, corps, pieces_jointes=None) -> EmailMessage:
    """pieces_jointes : [(nom du fichier, contenu en octets, type comme « application/zip »)]."""
    utilisateur = env["SMTP_USER"]
    m = EmailMessage()
    m["Subject"] = sujet
    m["From"] = env.get("SMTP_FROM") or utilisateur
    m["To"] = env.get("ALERT_EMAIL_TO") or utilisateur
    m[ENTETE_AGENTS] = "oui"   # pour que Le Passeur ne prenne pas un e-mail d'agent pour un signe de vie
    m.set_content(corps)
    for nom, contenu, genre in pieces_jointes or []:
        principal, _, secondaire = genre.partition("/")
        m.add_attachment(contenu, maintype=principal, subtype=secondaire or "octet-stream", filename=nom)
    return m


class ErreurEtape(Exception):
    """Une erreur d'envoi d'e-mail, avec l'étape où elle s'est produite (jamais de mot de passe dedans)."""


def _etape(nom, fonction, *args):
    try:
        return fonction(*args)
    except smtplib.SMTPAuthenticationError as exc:
        raise ErreurEtape(f"étape « {nom} » : Google refuse l'identifiant ou le mot de passe (code {exc.smtp_code})") from exc
    except Exception as exc:
        raise ErreurEtape(f"étape « {nom} » : {type(exc).__name__} : {exc}") from exc


def _envoyer_sur(hote, port, utilisateur, mot_de_passe, message):
    contexte = ssl.create_default_context()
    # arguments toujours nommés : les versions récentes de Python (3.12 et plus) refusent les arguments « en vrac »
    if port == 465:
        smtp = _etape("connexion chiffrée", lambda: smtplib.SMTP_SSL(hote, port, timeout=30, context=contexte))
    else:
        smtp = _etape("connexion", lambda: smtplib.SMTP(hote, port, timeout=30))
    try:
        if port != 465:
            _etape("chiffrement STARTTLS", smtp.starttls, contexte)
        _etape("identification", smtp.login, utilisateur, mot_de_passe)
        _etape("envoi du message", smtp.send_message, message)
    finally:
        try:
            smtp.close()
        except Exception:
            pass


def envoyer_email(env, msg):
    hote = env.get("SMTP_HOST") or "smtp.gmail.com"
    port_choisi = env.get("SMTP_PORT")
    utilisateur = env["SMTP_USER"]
    mot_de_passe = env["SMTP_PASSWORD"].replace(" ", "")  # Google affiche les mots de passe d'appli avec des espaces
    message = construire_email(env, msg["sujet"], msg["corps"], msg.get("pieces_jointes"))
    print(f"E-mail : serveur {hote}, identifiant de {len(utilisateur)} caractères, mot de passe de {len(mot_de_passe)} caractères "
          "(16 attendus pour un mot de passe d'application Google).")
    try:
        _envoyer_sur(hote, int(port_choisi or 465), utilisateur, mot_de_passe, message)
    except ErreurEtape as exc:
        refuse = "Google refuse" in str(exc)
        if port_choisi or refuse:      # une porte choisie à la main, ou un mot de passe refusé : l'autre porte n'y change rien
            raise
        # connexion coupée sur la porte 465 : on essaie l'autre porte de Gmail (587, chiffrement STARTTLS)
        print(f"Porte 465 inutilisable ({exc}) : nouvel essai sur la porte 587.")
        _envoyer_sur(hote, 587, utilisateur, mot_de_passe, message)


def envoyer_push(env, msg):
    serveur = (env.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    charge = {
        "topic": env["NTFY_TOPIC"],
        "title": msg["push_titre"],
        "message": msg["push_texte"],
        "priority": msg["priorite"],
        "tags": msg["tags"],
    }
    if msg.get("clic"):
        charge["click"] = msg["clic"]
    requete = urllib.request.Request(
        serveur + "/",
        data=json.dumps(charge).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(requete, timeout=20) as reponse:
        if not 200 <= reponse.status < 300:
            raise RuntimeError(f"ntfy a répondu {reponse.status}")


def url_sms(env, texte) -> str:
    return "https://smsapi.free-mobile.fr/sendmsg?" + urllib.parse.urlencode(
        {"user": env["FREE_SMS_USER"], "pass": env["FREE_SMS_KEY"], "msg": texte}
    )


def envoyer_sms(env, texte):
    with urllib.request.urlopen(url_sms(env, texte), timeout=20) as reponse:
        if reponse.status != 200:
            raise RuntimeError(f"Free Mobile a répondu {reponse.status}")


class Notifier:
    """Envoie les alertes sur tous les canaux configurés (un canal en panne ne bloque pas les autres)."""

    def __init__(self, env=None, dry_run=False):
        source = os.environ if env is None else env
        self.env = {k: (v or "").strip() for k, v in source.items() if isinstance(v, str) or v is None}
        self.dry_run = dry_run

    def canaux(self):
        canaux = []
        if self.env.get("SMTP_USER") and self.env.get("SMTP_PASSWORD"):
            canaux.append("e-mail")
        if self.env.get("NTFY_TOPIC"):
            canaux.append("push")
        if self.env.get("FREE_SMS_USER") and self.env.get("FREE_SMS_KEY"):
            canaux.append("sms")
        return canaux

    def envoyer(self, msg, avec_sms=False) -> bool:
        if self.dry_run:
            print("--- [SIMULATION] alerte non envoyée ---")
            print("Sujet :", msg["sujet"])
            print(msg["corps"])
            print("Push :", msg["push_titre"], "|", msg["push_texte"], f"(priorité {msg['priorite']})")
            if avec_sms and msg.get("sms"):
                print("SMS :", msg["sms"])
            print("--- fin de la simulation ---")
            return True
        canaux = self.canaux()
        if not canaux:
            print("ATTENTION : aucun canal d'alerte n'est configuré (voir le guide, partie « secrets »).")
            return False
        env_email = self.env
        t = transmission.etat(self.env)
        proches = transmission.contacts(self.env) if t else []
        if proches:
            # mode transmission : l'e-mail part aussi aux proches désignés, avec un bandeau d'explication
            msg = transmission.habiller(msg, t, " et ".join(c.get("nom", "?") for c in proches))
            moi = self.env.get("ALERT_EMAIL_TO") or self.env.get("SMTP_USER")
            env_email = dict(self.env, ALERT_EMAIL_TO=", ".join([moi] + [c["email"] for c in proches]))
        reussi = False
        if "e-mail" in canaux:
            try:
                envoyer_email(env_email, msg)
                print("Alerte envoyée par e-mail.")
                reussi = True
            except Exception as exc:  # noqa: BLE001
                print(f"Échec de l'envoi par e-mail : {type(exc).__name__} : {exc}")
        if "push" in canaux:
            try:
                envoyer_push(self.env, msg)
                print("Alerte envoyée par notification push.")
                reussi = True
            except Exception as exc:  # noqa: BLE001
                print(f"Échec de la notification push : {type(exc).__name__} : {exc}")
        if "sms" in canaux and avec_sms and msg.get("sms"):
            try:
                envoyer_sms(self.env, msg["sms"])
                print("Alerte envoyée par SMS.")
                reussi = True
            except Exception as exc:  # noqa: BLE001
                print(f"Échec de l'envoi du SMS : {type(exc).__name__}")
        return reussi
