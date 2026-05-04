# Triskell Core

**Bibliothèque partagée** pour l'écosystème Triskell Studio.

Tous les outils Triskell (Le Dénicheur, Sales Tunnel, AlphaBeast,
AlphaCast, Command Center) consomment ce noyau au lieu de dupliquer le code.

## Périmètre

```
triskell_core/
├── prospect/   ← sources, enrichers, CRM, outreach (ex-engine)
├── ai/         ← providers IA (Anthropic, OpenAI, Google, Mistral, xAI)
│                  + composeur de prompts
├── social/     ← (futur) adapters de publication LinkedIn/X/Bluesky/YouTube
├── crm/        ← (futur) CRM unifié si on extrait celui de prospect/
└── data/       ← assets statiques (mega_prompts.json, etc.)
```

## Installation locale (mode dev)

```bash
cd "Triskell Core"
pip install -e .
```

Puis dans n'importe quel projet (Le Dénicheur, Command Center…) :

```python
from triskell_core.prospect.sources import sirene
from triskell_core.ai.providers import send_to_provider
from triskell_core.ai.builder import build_ultimate_prompt
```

## Politique de versioning

- Le noyau évolue indépendamment des apps qui l'utilisent.
- Pas de breaking change sans version majeure.
- Les apps grand public (Le Dénicheur, etc.) peuvent rester sur une version
  ancienne du Core jusqu'à leur prochaine release.

## Voir aussi

- `Triskell Studio/COMMAND_CENTER/` — l'orchestrateur interne de Jordan (à venir)
- Apps qui utilisent (ou utiliseront) ce Core :
  - `Triskell 6 - Le Denicheur/`
  - `Prospection/triskell_sales_tunnel/`
  - `Prompts/ultimate_prompt_app/`
  - `Réseaux/` (en TS, branchera via API HTTP locale)
