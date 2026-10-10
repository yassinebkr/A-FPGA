# 7 — Technologie, corners et preuve de fréquence

## Du RTL à une vraie puce

Le RTL décrit un comportement logique. La technologie détermine les portes disponibles, leur vitesse, leur consommation, les mémoires, les cellules d'IO et les règles géométriques de fabrication.

Notre profil de simulation restera indépendant du procédé. La cible physique principale est IHP SG13G2, sous réserve que le fournisseur confirme les versions exactes acceptées pour le shuttle. Le PDK public contient les vues nécessaires à un flux numérique ouvert et conserve officiellement un statut preview.

- [IHP Open PDK](https://github.com/IHP-GmbH/IHP-Open-PDK)
- [Template numérique LibreLane](https://github.com/IHP-GmbH/ihp-sg13-librelane-template)
- [Processus Open-Silicon MPW](https://github.com/IHP-GmbH/Open-Silicon-MPW)

## Qu'est-ce qu'un corner PVT ?

PVT signifie procédé, voltage et température. Deux puces du même wafer ne possèdent jamais des transistors parfaitement identiques. La tension varie et la chaleur ralentit généralement certains chemins.

Les bibliothèques fournissent donc plusieurs modèles, par exemple transistors lents avec tension faible et température élevée. Le timing doit passer tous les corners requis, y compris le pire cas applicable.

## Fréquence et période

La période est le temps disponible entre deux fronts d'horloge :

| Fréquence | Période |
|---|---:|
| 100 MHz | 10 ns |
| 150 MHz | 6,67 ns |
| 200 MHz | 5 ns |
| 250 MHz | 4 ns |
| 500 MHz | 2 ns |

À 250 MHz, un calcul, son routage et les marges d'horloge doivent tenir en moins de 4 ns. Une SRAM répondant en 5 ns impose une étape supplémentaire, même si le cœur seul est assez rapide.

## Trois périmètres de mesure

Le cœur seul révèle le potentiel du pipeline. Le sous-système cœur, interconnexion et SRAM révèle les limites de mémoire. La puce complète ajoute périphériques, réseau d'horloge, pad-ring et alimentation.

Chaque fréquence sera testée dans cet ordre. Une régression peut alors être attribuée au bloc qui l'introduit.

## Niveaux de preuve

La synthèse transforme le RTL en portes et donne une première estimation. Le placement-routage positionne les portes, construit l'horloge et connecte les fils. L'extraction évalue les résistances et capacités réelles.

Une qualification physique vérifie ensuite :

- setup et hold sur tous les corners ;
- slew, capacité et fanout ;
- chemins temporels correctement contraints ;
- intégrité de l'horloge et de l'alimentation ;
- DRC, LVS et antenne ;
- équivalence entre RTL et netlist.

La mesure sur plusieurs puces, tensions et températures constitue la preuve finale de fréquence sur silicium.

## Pourquoi progresser par paliers ?

100 MHz fournit un contrôle comparable à Croc. 150 MHz valide la direction générale. 200 MHz teste sérieusement le pipeline et les mémoires. 250 MHz est l'objectif principal.

Un échec documenté déclenche une décision explicite : modifier l'architecture, choisir une autre macro mémoire ou changer de technologie. Le système ne réduit jamais silencieusement l'objectif.

## Le profil 500 MHz

Deux nanosecondes représentent un budget extrêmement serré pour une puce complète en 130 nm avec des bibliothèques ouvertes. Ce profil pourra demander un pipeline plus profond, des mémoires différentes, des cellules spécialisées ou un procédé plus avancé.

Les résultats à 200 et 250 MHz fourniront les données nécessaires pour choisir cette future technologie.
