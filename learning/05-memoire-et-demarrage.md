# 5 — La mémoire et la chaîne de démarrage

## Trois mémoires, trois rôles

Le MCU utilise plusieurs mémoires parce qu'elles répondent à des besoins différents :

- la **ROM interne** contient le premier code exécuté et ne change plus après fabrication ;
- la **Flash SPI externe** conserve le firmware lorsque l'alimentation est coupée ;
- la **SRAM interne** est rapide et contient le programme actif ainsi que ses données.

Au reset, la ROM lit l'image stockée en Flash, la contrôle, la copie en SRAM puis lui donne l'exécution.

## Pourquoi une ROM de 8 Kio ?

Elle doit contenir l'initialisation minimale, le pilote SPI de chargement, la validation de l'image, le test de mémoire et le mode de récupération. Une ROM trop petite rend chaque évolution difficile. Une grande ROM immobilise du silicium avec un contenu impossible à corriger après fabrication.

Le chargeur doit donc rester simple, auditable et accompagné d'un chemin de récupération JTAG.

## Pourquoi viser 128 Kio de SRAM ?

64 Kio suffisent à de petits programmes bare-metal et à certains usages FreeRTOS. 128 Kio permettent des démonstrations plus riches et des configurations minimales de Zephyr. 256 Kio augmenteraient sensiblement la surface sur un procédé 130 nm.

La cible de 128 Kio sera organisée en quatre banques de 32 Kio. Plusieurs banques facilitent le placement physique et raccourcissent certains fils. La configuration 64 Kio restera disponible si les macros réelles rendent la cible trop grande ou trop lente.

## Qu'est-ce qu'une macro SRAM ?

Une SRAM n'est généralement pas construite cellule par cellule par le RTL. Le procédé fournit des blocs mémoire caractérisés avec une taille, des broches, un délai, une consommation et des vues physiques précises.

Une enveloppe technologique présente la même interface au SoC et sélectionne la macro correspondant au procédé. Les simulations peuvent utiliser un modèle générique ; la fabrication emploie la macro qualifiée.

## Mémoire et fréquence

À 250 MHz, un cycle dure 4 ns. Si la SRAM répond en 6 ns, le cœur doit attendre un cycle supplémentaire ou utiliser un étage de pipeline. Le protocole à latence variable permet cette attente sans corrompre l'exécution.

La capacité d'un cœur à 250 MHz ne garantit donc pas que l'ensemble du MCU atteigne cette fréquence. Les macros mémoire font partie de la preuve physique.

## Pourquoi copier le firmware ?

Une Flash série transmet peu de bits à la fois et reste beaucoup plus lente que la SRAM. Exécuter directement depuis la Flash demanderait un cache et de la prélecture. Copier l'image au démarrage simplifie le premier silicium et rend ensuite l'exécution prévisible.

## Contrôler l'image avec CRC32

L'en-tête indique la version, la taille, l'adresse de chargement et le point d'entrée. Le CRC32 détecte une image endommagée. Il ne prouve pas qui a créé le firmware ; une future version sécurisée pourra ajouter une signature cryptographique et une gestion de clés.

## Tester la SRAM

La ROM écrit et relit des motifs conçus pour révéler des cellules bloquées ou des interactions entre cellules. Le résultat doit être visible par UART ou JTAG. Un futur MBIST matériel pourra accélérer et renforcer ce test si les exigences de fabrication le demandent.

L'ECC généralisé est différé. Une parité éventuelle dépendra des largeurs et délais des macros disponibles.
