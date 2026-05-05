# Prospection Engine — moteur multi-sources autonome

Pipeline complet de **recherche → enrichissement → envoi → relance**, complément
du **Triskell Sales Tunnel** (qui rédige les messages).

## Pourquoi ce moteur ?

Le Dénicheur (depuis v1.3.0) couvre 9 plateformes créateurs :
YouTube, Twitch, Reddit, Bluesky, Mastodon, Apple Podcasts, Dailymotion,
Kick, GitHub. Limites résiduelles :
- 100 résultats max par recherche YouTube
- Pas de source B2B / artisans / commerces (Sirene, Maps — gérés par Triskell Command)
- Le dédoublonnage cross-plateforme est désormais en place (cf. core/relevance.py)

Ce moteur résout chacun de ces points et ferme la boucle de prospection
**sans intervention humaine au quotidien**, sans toucher à l'app Le Dénicheur.

## Architecture

```
sources/        → Le Dénicheur (import), Sirene, Maps Places, Footprint
    ↓
core/prospect   → schéma unifié + dédoublonnage cross-source
    ↓
enrichers/      → fetch web, suit linktree, cross-ref Sirene ↔ site
    ↓
core/crm        → ~/.triskell-prospect/prospects.json (CRM unifié)
    ↓
outreach/       → SMTP sender + IMAP listener + templates
    ↓
nightly.py      → boucle 03:00 : poll → 1er envoi → relance J+5
```

## Données

```
~/.triskell-prospect/
├── prospects.json     ← CRM unifié (toutes sources)
├── enrich_cache/      ← cache HTML 7j
├── config.json        ← clés API + credentials SMTP/IMAP
├── send_log.json      ← compteur quotidien
├── imap_state.json    ← dernier UID IMAP traité
├── nightly.log        ← log de la boucle nocturne
└── templates.json     ← override des templates (optionnel)
```

Le CRM Le Dénicheur (`~/.ledenicheur/prospects.json`) reste **lecture seule**.

## Lancement

```bash
pip install -r requirements.txt
python -m engine.cli --help
```

## Cycle utilisateur typique

```bash
# 1. Lundi matin : nouvelle niche
python -m engine.cli search-sirene --naf 56.10A --departement 35 --effectif 00 --max 100
python -m engine.cli enrich --footprint --max 100

# 2. (Une fois pour toutes) config SMTP/IMAP via mot de passe d'app Gmail
python -m engine.cli config \
    --smtp-host smtp.gmail.com --smtp-port 587 \
    --smtp-user toi@gmail.com --smtp-password "mot_de_passe_app" \
    --from-email toi@gmail.com --from-name "Jordan — Triskell" \
    --imap-host imap.gmail.com --imap-user toi@gmail.com --imap-password "mot_de_passe_app"

# 3. (Une fois pour toutes) tâche Windows à 03:00 chaque jour
powershell -ExecutionPolicy Bypass -File engine/install_scheduled_task.ps1
```

La boucle nocturne fait, sans toi : **poll IMAP → envoi 40/jour → relance J+5**.
Tu n'interviens que quand un prospect répond (statut auto `replied`).

## Commandes CLI

| Commande | Rôle |
|---|---|
| `import-denicheur` | Importer le CRM de l'app Le Dénicheur |
| `search-sirene` | Chercher entreprises FR (NAF + dpt + effectif + date création) |
| `search-maps` | Chercher commerces/TPE via Google Maps Places (clé requise) |
| `enrich [--footprint]` | Visiter les sites + extraire emails/tél, valider par cross-ref |
| `list [--has-email] [--status X]` | Lire le CRM |
| `export --out X.csv` | Exporter |
| `send --template tpe_intro` | Envoyer une vague (1er contact ou `--follow-up`) |
| `poll-replies` | Détecter les réponses IMAP et basculer le statut |
| `config` | Stocker clés API + credentials |

## Coût récurrent

| Brique | Coût |
|---|---|
| Le Dénicheur, Sirene, Web Enricher, Linktree, Footprint | 0 € |
| Maps Places | 200 $/mois offerts par Google (≈ 6 000 requêtes) |
| SMTP Gmail | 0 € (jusqu'à 500/jour ; on cap à 40/jour pour la sécu) |
| IMAP Gmail | 0 € |
| **Total** | **0 €/mois** |
