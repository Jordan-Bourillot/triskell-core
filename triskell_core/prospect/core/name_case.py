"""Normalisation de la casse d'un nom d'affichage (français).

Contexte : ~1 % des fiches prospects ont un nom saisi/scrapé tout en
minuscules (« à la mesure du bois », « plomberie albert nicolas »).
Tel quel, il apparaît en toutes lettres dans les mails de prospection :
« voici à quoi pourrait ressembler le site de a la mesure du bois ».

La normalisation se fait AU RENDU du mail (jamais en base : on ne
réécrit pas une donnée d'origine de façon irréversible). Règles :

- On ne touche QUE les noms clairement fautifs = ENTIÈREMENT en
  minuscules. Un nom qui porte la moindre majuscule est laissé EXACTEMENT
  tel quel : casse mixte voulue (« l'Atelier de Maud », « iD Verde ») ET
  ALL CAPS légitimes (« SARL MOUGIN ») sont respectés.
- Casse « enseigne » : chaque mot important prend une majuscule, les
  petits mots (de, la, le, du, à, et…) restent en minuscule sauf en
  tête. Accent initial géré (à → À, é → É). Traits d'union et apostrophes
  d'élision gérés (marie-claire → Marie-Claire, l'instant → L'Instant).
- On n'aggrave jamais un nom « poubelle » scrapé (SEO à rallonge type
  « … electricien toulon var 83 depot ») : trop long / trop de mots → on
  le laisse tel quel.
"""

from __future__ import annotations

import re

__all__ = ["normalize_display_name", "fr_title_case"]


# Petits mots qui restent en minuscule (sauf en tête de nom / de segment).
_PETITS_MOTS = frozenset({
    "a", "à", "au", "aux", "de", "des", "du", "d", "la", "le", "les", "l",
    "et", "ou", "en", "dans", "sur", "sous", "par", "pour", "avec", "sans",
    "chez", "vers", "entre", "contre", "un", "une",
})

# Articles élidés : le morceau AVANT une apostrophe reste en minuscule
# hors tête (« restaurant l'instant » → « Restaurant l'Instant »).
_ELISIONS = frozenset({
    "d", "l", "j", "n", "m", "t", "s", "c", "qu", "jusqu", "lorsqu",
    "puisqu", "quoiqu",
})

# Au-delà, ce n'est plus un nom mais une description scrapée : on ne
# retouche pas (risque d'aggraver un faux positif).
_MAX_WORDS = 6
_MAX_LEN = 55

_APOSTROPHES = ("'", "’")  # droite et typographique


def _upcap(seg: str) -> str:
    """Majuscule sur la 1re lettre d'un segment, reste inchangé."""
    if not seg:
        return seg
    return seg[:1].upper() + seg[1:]


def _title_apostrophe_part(part: str, is_head: bool) -> str:
    """Casse d'un morceau pouvant contenir une apostrophe.

    On capitalise le morceau qui SUIT une apostrophe uniquement quand
    celui qui précède est un article élidé français (l', d', qu'…) :
    « l'instant » → « L'Instant », mais « dilya's » (possessif anglais)
    → « Dilya's », pas « Dilya'S ».
    """
    if not any(a in part for a in _APOSTROPHES):
        return _upcap(part)
    segs = re.split(r"(['’])", part)
    out: list[str] = []
    prev_text: str | None = None   # dernier segment-texte (minuscule)
    text_idx = 0
    for seg in segs:
        if seg in _APOSTROPHES:
            out.append(seg)
            continue
        if text_idx == 0:
            # Morceau AVANT la 1re apostrophe = article élidé potentiel.
            if not is_head and seg.lower() in _ELISIONS:
                out.append(seg.lower())          # « l' », « d' » hors tête
            else:
                out.append(_upcap(seg))
        elif prev_text in _ELISIONS:
            out.append(_upcap(seg))              # nom après élision → majuscule
        else:
            out.append(seg)                      # « 's » possessif → inchangé
        prev_text = seg.lower()
        text_idx += 1
    return "".join(out)


def _title_word(word: str, is_first: bool) -> str:
    """Casse d'un mot — les traits d'union sont traités comme un mini-titre
    (« saint-jean-de-luz » → « Saint-Jean-de-Luz »)."""
    parts = word.split("-")
    out: list[str] = []
    for j, part in enumerate(parts):
        is_head = is_first and j == 0
        if not is_head and part.lower() in _PETITS_MOTS:
            out.append(part.lower())
        else:
            out.append(_title_apostrophe_part(part, is_head))
    return "-".join(out)


def _is_all_lowercase(s: str) -> bool:
    """True si le texte a des lettres et AUCUNE majuscule."""
    if not any(c.isalpha() for c in s):
        return False
    return not any(c.isupper() for c in s)


def _looks_like_garbage(s: str) -> bool:
    """True si le nom ressemble à une description scrapée (à ne pas retoucher)."""
    return len(s) > _MAX_LEN or len(s.split()) > _MAX_WORDS


def normalize_display_name(name: str) -> str:
    """Remet une casse « enseigne » sur un nom ENTIÈREMENT en minuscules.

    Ne modifie que les noms clairement fautifs. Renvoie le nom inchangé
    sinon (casse mixte, ALL CAPS, vide, sans lettre, ou « poubelle »).
    """
    if not name:
        return name or ""
    s = name.strip()
    if not s:
        return name
    if not _is_all_lowercase(s):
        return name          # casse mixte ou ALL CAPS voulue → on respecte
    if _looks_like_garbage(s):
        return name          # description scrapée → on n'aggrave pas
    words = s.split()
    return " ".join(_title_word(w, i == 0) for i, w in enumerate(words))


# Alias explicite (même fonction, nom parlant côté appelants).
fr_title_case = normalize_display_name
