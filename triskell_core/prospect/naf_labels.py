"""Traduit un code d'activité (NAF/APE) en libellé lisible en français.

L'API publique `recherche-entreprises.api.gouv.fr` ne renvoie PAS le libellé
du métier : seulement le code (« 47.76Z ») et la lettre de section (« G »).
Résultat avant correction : les fiches du Chasseur affichaient « G » comme
métier (observé en vrai le 13/06/2026 sur une chasse fleuristes).

On traduit donc nous-mêmes :
  1. table ciblée des codes les plus courants en prospection PME locale
     (commerces, artisans du bâtiment, restauration, beauté, santé, services) ;
  2. à défaut, libellé de la SECTION (lettre A–U) — toujours plus parlant
     qu'une lettre seule ;
  3. à défaut, le code brut (mieux que rien).

Déterministe, sans réseau, sans IA.
"""
from __future__ import annotations

# Libellés officiels des 21 sections NAF rév. 2 (lettre → libellé).
SECTION_LABELS: dict[str, str] = {
    "A": "Agriculture, sylviculture et pêche",
    "B": "Industries extractives",
    "C": "Industrie manufacturière",
    "D": "Production et distribution d'électricité et de gaz",
    "E": "Production et distribution d'eau, assainissement, déchets",
    "F": "Construction",
    "G": "Commerce ; réparation d'automobiles et de motocycles",
    "H": "Transports et entreposage",
    "I": "Hébergement et restauration",
    "J": "Information et communication",
    "K": "Activités financières et d'assurance",
    "L": "Activités immobilières",
    "M": "Activités spécialisées, scientifiques et techniques",
    "N": "Activités de services administratifs et de soutien",
    "O": "Administration publique",
    "P": "Enseignement",
    "Q": "Santé humaine et action sociale",
    "R": "Arts, spectacles et activités récréatives",
    "S": "Autres activités de services",
    "T": "Activités des ménages en tant qu'employeurs",
    "U": "Activités extra-territoriales",
}

# Codes NAF les plus fréquents parmi les cibles de prospection PME locale.
# Libellés volontairement courts et parlants (entre parenthèses : le métier
# courant quand le libellé officiel est moins évident).
NAF_LABELS: dict[str, str] = {
    # --- Construction / artisans du bâtiment ---
    "41.20A": "Construction de maisons individuelles",
    "41.20B": "Construction d'autres bâtiments",
    "43.11Z": "Travaux de démolition",
    "43.12A": "Terrassements courants",
    "43.12B": "Terrassements et préparation des sols",
    "43.13Z": "Forages et sondages",
    "43.21A": "Travaux d'installation électrique (électricien)",
    "43.21B": "Installation électrique sur la voie publique",
    "43.22A": "Installation d'eau et de gaz (plombier)",
    "43.22B": "Installation de chauffage et climatisation (chauffagiste)",
    "43.29A": "Travaux d'isolation",
    "43.29B": "Autres travaux d'installation",
    "43.31Z": "Travaux de plâtrerie (plaquiste)",
    "43.32A": "Menuiserie bois et PVC",
    "43.32B": "Menuiserie métallique et serrurerie",
    "43.32C": "Agencement de lieux de vente",
    "43.33Z": "Revêtement des sols et des murs (carreleur)",
    "43.34Z": "Travaux de peinture et vitrerie (peintre)",
    "43.39Z": "Autres travaux de finition",
    "43.91A": "Travaux de charpente",
    "43.91B": "Travaux de couverture (couvreur)",
    "43.99A": "Travaux d'étanchéité",
    "43.99B": "Montage de structures métalliques",
    "43.99C": "Maçonnerie générale et gros œuvre (maçon)",
    "43.99D": "Autres travaux spécialisés de construction",
    "43.99E": "Location de matériel de construction avec opérateur",
    "71.11Z": "Activités d'architecture",
    "71.12B": "Ingénierie, études techniques",
    "81.30Z": "Aménagement paysager (paysagiste)",
    # --- Alimentation / commerces de bouche ---
    "10.71C": "Boulangerie et boulangerie-pâtisserie",
    "10.71D": "Pâtisserie",
    "10.13B": "Charcuterie",
    "47.11A": "Commerce d'alimentation générale",
    "47.11B": "Supérette",
    "47.11C": "Supermarché",
    "47.21Z": "Commerce de fruits et légumes (primeur)",
    "47.22Z": "Commerce de viandes (boucherie)",
    "47.23Z": "Commerce de poissons et crustacés (poissonnerie)",
    "47.24Z": "Commerce de pain, pâtisserie et confiserie",
    "47.25Z": "Commerce de boissons (caviste)",
    "47.26Z": "Commerce de produits à base de tabac",
    "47.29Z": "Autres commerces alimentaires spécialisés",
    # --- Commerces de détail divers ---
    "47.71Z": "Commerce d'habillement (prêt-à-porter)",
    "47.72A": "Commerce de chaussures",
    "47.72B": "Commerce de maroquinerie",
    "47.73Z": "Commerce de produits pharmaceutiques (pharmacie)",
    "47.74Z": "Commerce d'articles médicaux et orthopédiques",
    "47.75Z": "Commerce de parfumerie et produits de beauté",
    "47.76Z": "Commerce de fleurs, plantes et animaux de compagnie",
    "47.78C": "Autres commerces de détail spécialisés",
    # --- Hébergement / restauration ---
    "55.10Z": "Hôtels et hébergement similaire",
    "55.20Z": "Hébergement touristique de courte durée",
    "56.10A": "Restauration traditionnelle",
    "56.10B": "Cafétérias et libres-services",
    "56.10C": "Restauration rapide",
    "56.21Z": "Services des traiteurs",
    "56.29A": "Restauration collective sous contrat",
    "56.30Z": "Débits de boissons (bar, café)",
    # --- Beauté / bien-être / sport ---
    "96.02A": "Coiffure",
    "96.02B": "Soins de beauté (institut)",
    "96.04Z": "Entretien corporel (spa, bien-être)",
    "96.09Z": "Autres services personnels",
    "93.12Z": "Activités de clubs de sports",
    "93.13Z": "Centres de culture physique (salle de sport)",
    "85.51Z": "Enseignement du sport et des loisirs",
    # --- Santé ---
    "86.21Z": "Médecins généralistes",
    "86.22C": "Médecins spécialistes",
    "86.23Z": "Pratique dentaire",
    "86.90D": "Infirmiers et sages-femmes",
    "86.90E": "Rééducation, kinésithérapie",
    "86.90F": "Autres activités de santé humaine",
    "75.00Z": "Activités vétérinaires",
    # --- Automobile ---
    "45.11Z": "Commerce de voitures et véhicules légers",
    "45.20A": "Entretien et réparation de véhicules légers (garage)",
    "45.20B": "Entretien et réparation d'autres véhicules",
    "45.32Z": "Commerce d'équipements automobiles",
    # --- Services aux entreprises / professions ---
    "68.31Z": "Agences immobilières",
    "69.10Z": "Activités juridiques",
    "69.20Z": "Activités comptables",
    "70.21Z": "Conseil en communication et relations publiques",
    "70.22Z": "Conseil en gestion d'entreprise",
    "73.11Z": "Agences de publicité",
    "74.10Z": "Activités de design",
    "74.20Z": "Activités photographiques (photographe)",
    "74.30Z": "Traduction et interprétation",
    "62.01Z": "Programmation informatique",
    "62.02A": "Conseil en systèmes et logiciels",
    "63.11Z": "Traitement de données, hébergement web",
    "79.11Z": "Agences de voyage",
    "81.21Z": "Nettoyage courant des bâtiments",
    "81.22Z": "Nettoyage spécialisé des bâtiments",
    # --- Réparation ---
    "95.21Z": "Réparation de produits électroniques grand public",
    "95.22Z": "Réparation d'appareils électroménagers",
    "95.23Z": "Réparation de chaussures et articles en cuir",
    "95.24Z": "Réparation de meubles",
    "95.25Z": "Réparation d'horlogerie et de bijouterie",
    "95.29Z": "Réparation d'autres biens personnels",
    # --- Enseignement / formation ---
    "85.53Z": "Enseignement de la conduite (auto-école)",
    "85.59A": "Formation continue d'adultes",
    "85.59B": "Autres enseignements",
}


# Mots-clés métier par code NAF : au moins UN doit apparaître sur le site
# d'une entreprise réellement de ce métier. Sert de garde-fou de pertinence
# côté Chasseur (une « SARL FLEUR'IMMO » mal classée fleuriste au registre,
# mais dont le site parle d'immobilier, sera écartée). Tous en minuscules,
# comparés en sous-chaîne. Volontairement GÉNÉREUX (synonymes) pour ne jamais
# écarter un vrai prospect. Un métier ABSENT de cette table = aucun garde-fou.
SECTOR_KEYWORDS: dict[str, frozenset[str]] = {
    "47.76Z": frozenset({"fleuriste", "fleurs", "bouquet", "floral",
                          "fleuri", "orchid", "compositions"}),
    "10.71C": frozenset({"boulanger", "pâtiss", "patiss", "pain",
                         "viennoiser", "baguette", "fournil"}),
    "10.71D": frozenset({"pâtiss", "patiss", "gâteau", "gateau", "macaron",
                         "chocolat"}),
    "47.24Z": frozenset({"boulanger", "pâtiss", "patiss", "pain", "fournil"}),
    "47.22Z": frozenset({"boucher", "charcuter", "viande", "traiteur"}),
    "47.23Z": frozenset({"poissonner", "poisson", "fruits de mer", "marée",
                         "crustacé"}),
    "47.21Z": frozenset({"primeur", "fruits", "légumes", "legumes",
                         "maraîch", "maraich"}),
    "47.25Z": frozenset({"caviste", "vins", "vin ", "spiritueux", "champagne"}),
    "47.73Z": frozenset({"pharmacie", "pharmacien", "médicament",
                         "ordonnance", "parapharmacie"}),
    "47.75Z": frozenset({"parfum", "cosmétique", "cosmetique", "beauté",
                         "maquillage"}),
    "43.21A": frozenset({"électric", "electric", "domotique", "luminaire",
                         "installation électrique"}),
    "43.22A": frozenset({"plomb", "sanitaire", "chauffage", "chaudièr",
                         "chaudier", "fuite", "salle de bain"}),
    "43.22B": frozenset({"chauffage", "climatis", "pompe à chaleur",
                         "chaudièr", "chaudier", "thermique"}),
    "43.31Z": frozenset({"plaquiste", "plâtre", "platre", "placo", "cloison",
                         "isolation", "plâtrerie", "platrerie"}),
    "43.32A": frozenset({"menuis", "fenêtre", "fenetre", "porte", "volet",
                         "escalier", "agencement", "dressing"}),
    "43.33Z": frozenset({"carrel", "faïence", "faience", "mosaïque",
                         "mosaique", "revêtement", "revetement"}),
    "43.34Z": frozenset({"peinture", "peintre", "ravalement", "enduit",
                         "décoration", "decoration"}),
    "43.39Z": frozenset({"rénovation", "renovation", "finition", "décoration",
                         "decoration"}),
    "43.91B": frozenset({"couvreur", "couverture", "toiture", "toit",
                         "zinguerie", "charpente"}),
    "43.99C": frozenset({"maçon", "macon", "maçonnerie", "maconnerie",
                         "gros œuvre", "gros oeuvre", "dalle", "parpaing",
                         "fondation"}),
    "41.20A": frozenset({"construction", "maison", "constructeur",
                         "bâtiment", "batiment", "rénovation", "renovation"}),
    "41.20B": frozenset({"construction", "bâtiment", "batiment", "chantier"}),
    "71.11Z": frozenset({"architect", "maîtrise d'œuvre", "permis de construire",
                         "plans"}),
    "81.30Z": frozenset({"paysag", "jardin", "espaces verts", "élagage",
                         "elagage", "gazon", "plantation", "tonte"}),
    "56.10A": frozenset({"restaurant", "menu", "carte", "réserv", "reserv",
                         "brasserie", "cuisine", "midi", "plat"}),
    "56.10B": frozenset({"restaurant", "menu", "self", "libre-service",
                         "cuisine"}),
    "56.10C": frozenset({"restaurant", "burger", "pizza", "kebab", "tacos",
                         "snack", "fast", "à emporter", "a emporter"}),
    "56.21Z": frozenset({"traiteur", "réception", "reception", "buffet",
                         "événement", "evenement", "cocktail"}),
    "56.30Z": frozenset({"bar", "brasserie", "cocktail", "bière", "biere",
                         "pub", "apér"}),
    "55.10Z": frozenset({"hôtel", "hotel", "chambre", "réservation",
                         "reservation", "séjour", "sejour", "nuitée"}),
    "96.02A": frozenset({"coiff", "cheveux", "brushing", "barbier",
                         "coloration", "salon"}),
    "96.02B": frozenset({"institut", "beauté", "beaute", "esthétic",
                         "esthetic", "épilation", "epilation", "manucure",
                         "onglerie", "soin"}),
    "96.04Z": frozenset({"spa", "massage", "bien-être", "bien être",
                         "bien etre", "modelage", "sauna", "hammam",
                         "détente", "detente"}),
    "93.13Z": frozenset({"salle de sport", "fitness", "musculation",
                         "coach", "remise en forme", "cours collectifs"}),
    "74.20Z": frozenset({"photo", "photograph", "shooting", "reportage",
                         "portrait"}),
    "45.20A": frozenset({"garage", "automobile", "mécanique", "mecanique",
                         "réparation", "reparation", "vidange", "pneu",
                         "carrosserie", "entretien"}),
    "45.11Z": frozenset({"voiture", "véhicule", "vehicule", "occasion",
                         "concession", "automobile"}),
    "86.23Z": frozenset({"dentaire", "dentiste", "implant", "orthodont",
                         "couronne"}),
    "86.21Z": frozenset({"médecin", "medecin", "docteur", "consultation",
                         "cabinet médical", "généraliste", "generaliste"}),
    "86.90E": frozenset({"kinésithérap", "kinesitherap", "kiné", "kine",
                         "rééducation", "reeducation", "ostéo", "osteo"}),
    "75.00Z": frozenset({"vétérinaire", "veterinaire", "clinique",
                         "animaux", "animal"}),
    "70.22Z": frozenset({"conseil", "consulting", "accompagnement",
                         "stratégie", "strategie", "gestion", "coaching"}),
    "69.20Z": frozenset({"comptab", "expert-comptable", "expertise comptable",
                         "fiscal", "bilan"}),
    "68.31Z": frozenset({"immobilier", "immo", "agence", "vente", "location",
                         "appartement", "maison à vendre", "estimation"}),
}


def sector_keywords(code: str) -> frozenset[str]:
    """Mots-clés métier pour un code NAF (vide si métier non couvert)."""
    return SECTOR_KEYWORDS.get(_canon(code), frozenset())


def _canon(code: str) -> str:
    """Normalise un code NAF vers la forme pointée « 47.76Z »."""
    if not code:
        return ""
    c = code.strip().upper().replace(" ", "").replace(".", "")
    if len(c) >= 4 and c[:2].isdigit():
        return c[:2] + "." + c[2:]
    return c


def naf_label(code: str, section: str = "") -> str:
    """Renvoie un libellé lisible pour un code NAF.

    Ordre : table ciblée → libellé de section (lettre A–U) → code brut.
    Ne lève jamais : à défaut de tout, renvoie le code (ou "").
    """
    canon = _canon(code)
    if canon in NAF_LABELS:
        return NAF_LABELS[canon]
    sect = (section or "").strip().upper()[:1]
    if sect in SECTION_LABELS:
        return SECTION_LABELS[sect]
    return code or ""


__all__ = ["SECTION_LABELS", "NAF_LABELS", "naf_label",
           "SECTOR_KEYWORDS", "sector_keywords"]
