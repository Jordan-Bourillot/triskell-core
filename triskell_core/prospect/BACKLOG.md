# BACKLOG — Prospection Engine

Hors scope MVP. Classés par valeur business descendante.

## Sources additionnelles

- **PagesJaunes scraper** — Playwright headless, rate-limit doux, RGPD ok pour B2B. Source majeure FR pour artisans avec téléphone systématique. Fragile aux refontes du site, mais combinée avec Maps Places, couverture quasi totale du marché TPE FR.
- **RSS / Google Alerts** — surveille des keywords de niche pour détecter les nouveaux entrants ("ouverture café Rennes" + ville). Signal d'intention fort (early adopter).
- **LinkedIn API officielle** — uniquement pour Triskell en tant qu'éditeur (pas de scraping prospect). Permet d'envoyer des Sales Navigator messages à grande échelle. Réservé aux clients du tier "Sales Solutions".

## Enrichissement

- **Validation par appel téléphonique automatique** — service Twilio/Aircall pour vérifier qu'un numéro répond avant d'envoyer un mail. ~0,01 €/appel.
- **Détection d'ICP par IA** — appel Haiku par batch de 20 prospects pour scorer la pertinence niche↔prospect. ~0,002 $/prospect (budget négligeable mais effet x2 sur le taux de réponse).
- **Suivi des liens "About" YouTube** — l'API expose `featuredChannelsUrls` qu'on n'utilise pas. À grappiller pour les créateurs.
- **Détection langue** par contenu, pas seulement par champ `language` de la plateforme (souvent vide).

## CRM / orchestration

- **Dédoublonnage par fuzzy name** — si deux prospects ont (nom similaire + même ville) sans aucune clé contact, les fusionner (Levenshtein < 3 sur le slug).
- **Statut "junk"** automatique pour les prospects clairement hors ICP (gros groupes, multinationales) — détection sur effectif > 50 ou présence dans une whitelist ICP.
- **Score commercial unifié** — actuellement chaque source a son score interne. Construire un score [0–100] mêlant : taille (TPE bonus), récence Sirene, présence email pro, mentions légales OK, monétisation absente.
- **Recyclage des refus à 6 mois** — un prospect en `refused` peut redevenir `qualified` 6 mois plus tard si le contexte a changé.

## Outreach / canaux

- **Détection bounces (NDR parsing)** — parse les Non-Delivery Reports pour invalider des emails et corriger le score. Aujourd'hui on log juste `email_failed`.
- **A/B testing de templates** — renvoyer chaque variante à 50 % du segment et tracker le `replied` rate. Demande un compteur dans `prospect.history`.
- **Suivi des ouvertures** — pixel tracker dans le mail (Gmail le bloque, mais Outlook/Thunderbird le suivent). Optionnel, RGPD-borderline.
- **Rotation d'expéditeurs** — au-delà de 50 envois/jour, basculer entre 2-3 boîtes pour ne pas être flaggé spam.
- **Templates dynamiques par secteur** — actuellement un seul template par défaut. Idéalement : 1 template par couple (produit Triskell × ICP), généré depuis le Sales Tunnel et stocké dans templates.json.

## DX

- **Tests unitaires** dans `engine/tests/` (extracteur regex, normalisations, dédoublonnage, cross-ref). Le test live AFNIC reste manuel.
- **Logging structuré JSON** plutôt que `logging.WARNING` actuel.
- **Dashboard local** (Streamlit ou tk simple) : KPIs en temps réel — emails envoyés, replies, taux par template/source.
- **Mode `engine.cli pipeline`** qui chaîne `search → enrich → send` en une commande pour onboarding facile.
- **Bouton "Connect Gmail OAuth"** dans le Sales Tunnel — éviter au user de manipuler un mot de passe d'application.

## Hors scope définitif

- Instagram / TikTok scraping (ban + RGPD)
- LinkedIn Sales Navigator alternatives non-officielles (zone grise)
- Twitter/X API (payante, ~100 €/mois pour rien d'utile)
