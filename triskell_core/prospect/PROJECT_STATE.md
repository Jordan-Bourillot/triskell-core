# PROJECT_STATE — Prospection Engine

## Phase courante
**Phase 2 — Boucle d'envoi autonome livrée**, validée le 2026-05-04.

Le moteur ferme désormais la boucle complète : recherche → enrichissement →
qualification → envoi → relance → détection des réponses, sans intervention
humaine au quotidien.

## Briques en place

| Couche | Module | Fonction | Coût |
|---|---|---|---|
| **Sources** | `sources/denicheur.py` | Importe ~/.ledenicheur/prospects.json (lecture seule) | 0 € |
| | `sources/sirene.py` | API publique `recherche-entreprises` (NAF + dpt + effectif + date) | 0 € |
| | `sources/maps.py` | Google Places API (TPE avec téléphone/adresse) | 200 $/mois offerts |
| **Enrichissement** | `enrichers/web.py` | Fetch site + emails/tél/adresse + SIREN/SIRET/CP + mentions légales | 0 € |
| | `enrichers/linktree.py` | Suit linktr.ee/beacons.ai/etc. → vraies destinations | 0 € |
| | `enrichers/footprint.py` | Devine `<slug>.fr/.com` + HEAD si pas de site connu | 0 € |
| **Cross-ref** | `cli.py:_cross_ref` | Valide site ↔ Sirene par SIREN, code postal, ou email-domaine match | 0 € |
| **CRM** | `core/prospect.py` | Schéma unifié + dédoublonnage cross-source | 0 € |
| | `core/crm.py` | Persistance JSON + index match_keys + upsert | 0 € |
| **Envoi** | `outreach/templates.py` | Rendu de templates (depuis Sales Tunnel ou ~/.triskell-prospect/templates.json) | 0 € |
| | `outreach/smtp_sender.py` | SMTP Gmail/OVH, daily cap, pause humaine 30-120 s, log d'envoi | 0 € (Gmail gratuit) |
| | `outreach/imap_listener.py` | Détecte les réponses → bascule status `replied`, stoppe relances | 0 € |
| **Orchestration** | `nightly.py` | Boucle nocturne : poll → 1er contact → relance | 0 € |
| | `install_scheduled_task.ps1` | Installe la tâche Windows 03:00/jour | 0 € |
| **CLI** | `cli.py` | 9 commandes : `import-denicheur`, `search-sirene`, `search-maps`, `enrich`, `list`, `export`, `send`, `poll-replies`, `config` | — |

## Données

```
~/.triskell-prospect/
├── prospects.json     ← CRM unifié (toutes sources)
├── enrich_cache/      ← cache HTML 7j (sha256 par URL)
├── config.json        ← clés API + credentials SMTP/IMAP
├── send_log.json      ← compteur quotidien d'envois
├── imap_state.json    ← dernier UID IMAP traité (anti-rescan)
├── nightly.log        ← log de la boucle nocturne
└── templates.json     ← override des templates (optionnel)
```

## Stack
- Python 3.10+ (testé Python 3.12.x)
- `requests`, `beautifulsoup4`, `lxml`
- Stdlib : `smtplib`, `imaplib`, `email` (envoi/réception sans framework)
- 0 base de données, 0 serveur, 0 service récurrent payant
- Politesse HTTP : 1 req/sec/domaine, robots.txt respecté
- Politesse SMTP : pause aléatoire 30-120 s entre envois, daily cap

## Métriques observées (test live, 15 électriciens 35, effectif 0)
- Sirene → import : 15/15 (100 %)
- Footprint → site web trouvé : 8/15 (53 %)
- Cross-ref → faux positifs filtrés : 3 sites rejetés (PROTEC, AVF, JD2M…)
- Web Enricher → email pro extrait : 1 cas vérifié `site_verified` (ECO.PROTECH)
- Bout-en-bout dry-run send : 1 mail rendu correctement, prêt à envoyer

## Décisions structurantes
- **Identité = liste de match_keys**, pas un seul ID. Fusion cross-source.
- **Cross-ref par preuve positive uniquement** : on ne rejette qu'avec un signal contraire (autre SIREN affiché, codes postaux qui ne match pas du tout). L'absence de signal donne `low` + tag `site_unverified` plutôt que rejet.
- **Email du domaine + slug du nom = preuve forte** : permet de valider ECO.PROTECH même sans SIREN explicite sur le site.
- **Statuts sortants verrouillés** : `replied/won/lost/refused` ne peuvent pas être réécrits par un import ou un re-enrichissement.
- **Dry-run partout** sur les opérations qui touchent l'extérieur (envoi mail, écriture file).
- **Cache HTML 7j obligatoire** : un re-run rapide après ajustement n'écrase pas les sites visités.
- **Lecture seule sur ~/.ledenicheur** : on n'écrase jamais le CRM Le Dénicheur, on l'importe.

## Configuration nécessaire pour le mode autonome complet

| Brique | Config requise | Comment |
|---|---|---|
| Sirene | aucune | marche immédiatement |
| Le Dénicheur | aucune | si l'app a déjà un prospects.json |
| Footprint | aucune | marche immédiatement |
| Web/Linktree | aucune | marche immédiatement |
| **Maps Places** | clé API Google Cloud (Places API New activée) | `cli config --google-places-api-key XXX` |
| **SMTP** | mot de passe d'application Gmail (16 chars) | `cli config --smtp-host smtp.gmail.com --smtp-port 587 --smtp-user moi@gmail.com --smtp-password XXXX --from-email moi@gmail.com` |
| **IMAP** | même mot de passe d'application Gmail | `cli config --imap-host imap.gmail.com --imap-user moi@gmail.com --imap-password XXXX` |
| **Tâche planifiée** | rien — `powershell -ExecutionPolicy Bypass -File install_scheduled_task.ps1` | Installe le run nightly à 03:00 |

## Cycle utilisateur typique (post-config)

```bash
# Lundi matin : nouvelle niche
python -m engine.cli search-sirene --naf 56.10A --departement 35 --effectif 00 --max 100
python -m engine.cli enrich --footprint --max 100 --no-emails-only

# (la tâche planifiée fait le reste : 03:00/jour)
#   → poll IMAP (replies)
#   → 1er envoi à 40 prospects/jour
#   → relance J+5 sur les non-répondants

# Vendredi : check des réponses arrivées
python -m engine.cli list --status replied
```

## Variables d'env
Aucune. Tout passe par `~/.triskell-prospect/config.json`.

## Roadmap
**Phases 0+1+2 livrées (10/10 briques)**. Reste dans le BACKLOG des items
hors scope MVP : analytics de campagne, intégration LinkedIn API, etc.
