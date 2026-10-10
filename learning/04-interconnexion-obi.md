# 4 — L'interconnexion OBI

## Pourquoi un bus ?

Le cœur, les mémoires et les périphériques sont des blocs distincts. Le bus décrit comment un maître demande une lecture ou une écriture et comment une cible répond.

Une requête transporte une adresse, le type d'accès, les données à écrire et les octets concernés. La réponse transporte les données lues ou une erreur.

## Pourquoi OBI ?

OBI est une interface légère employée par CVE2 et CV32E40P. Elle permet d'attendre une cible lente avec moins d'états et de complexité qu'un grand protocole de SoC. Elle facilite aussi la comparaison avec Croc.

Le cœur dispose de chemins séparés pour les instructions et les données. Il peut ainsi lire l'instruction suivante pendant qu'une instruction précédente accède à la SRAM.

## Arbitrage et timing

Deux maîtres peuvent demander la même mémoire. Un arbitre choisit l'ordre. Ses règles doivent être déterministes, garantir une progression et empêcher une attente infinie.

Un multiplexeur reliant tous les blocs formerait un long chemin électrique. Des registres intermédiaires coupent ce chemin et peuvent ajouter un cycle. Le handshake OBI conserve la correction malgré cette latence variable.

## Périphériques mappés en mémoire

Une écriture C vers une adresse spéciale devient une transaction vers un registre UART, SPI, I²C, GPIO ou timer. Une source centrale produira les constantes RTL, les en-têtes C, la documentation et les tests de décodage. Le matériel et le logiciel partageront ainsi la même carte.

L'emplacement d'extension recevra des commandes comme cible OBI et pourra lever une interruption. Une future capacité maître lui permettra d'accéder à la SRAM après ajout des protections et tests d'arbitrage nécessaires.

Toute adresse inconnue doit produire une erreur explicite. Une politique de timeout doit aussi transformer une cible bloquée en panne diagnosticable.
