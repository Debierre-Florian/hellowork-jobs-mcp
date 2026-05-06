import logging
import os
from urllib.parse import urlencode

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from .scraper import HelloWorkScraper

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

mcp = FastMCP(
    name="HelloWork Jobs Search",
    instructions="""
    Tu peux rechercher des offres d'emploi sur HelloWork.com,
    le premier jobboard généraliste français avec 800 000+ offres.
    Utilise search_jobs pour trouver des offres par mots-clés et localisation.
    HelloWork est particulièrement fort sur les offres en France :
    CDI, CDD, alternance, stage, intérim, indépendant.
    Les résultats incluent souvent les salaires, contrairement à LinkedIn.
    Présente toujours les résultats de façon structurée et lisible.
    """,
)

scraper = HelloWorkScraper()


def _build_search_link(keywords: str, location: str) -> str:
    params = {"k": keywords, "l": location, "ray": "all"}
    return f"https://www.hellowork.com/fr-fr/emploi/recherche.html?{urlencode(params)}"


@mcp.tool()
async def search_jobs(
    keywords: str,
    location: str = "France",
    contract_type: str = "all",
    remote: str = "all",
    limit: int = 10,
) -> str:
    """
    Recherche des offres d'emploi sur HelloWork.com.

    Args:
        keywords: Mots-clés de recherche (ex: "responsable transport", "data analyst python")
        location: Ville ou région (ex: "Paris", "Lyon", "Bordeaux", "France")
        contract_type: Type de contrat - "all", "cdi", "cdd", "alternance", "stage", "interim", "independant"
        remote: Mode de travail - "all", "remote", "hybrid", "on_site"
        limit: Nombre de résultats souhaités (1-25, défaut: 10)

    Returns:
        Liste formatée des offres d'emploi trouvées
    """
    limit = max(1, min(limit, 25))
    contract_label = contract_type.upper() if contract_type != "all" else "Tous contrats"
    remote_label = {"all": "Tous modes", "remote": "Télétravail", "hybrid": "Hybride", "on_site": "Sur site"}.get(
        remote, remote
    )

    header = (
        f"## Offres HelloWork — {keywords} | {location}\n"
        f"**Contrat :** {contract_label} | **Mode :** {remote_label} | **Limite :** {limit}\n\n"
    )

    jobs = await scraper.search(
        keywords=keywords,
        location=location,
        contract_type=contract_type,
        remote=remote,
        limit=limit,
    )

    if not jobs:
        search_link = _build_search_link(keywords, location)
        return (
            header
            + "Aucune offre trouvée sur HelloWork pour ces critères.\n\n"
            + "**Suggestions :**\n"
            + "- Élargir la localisation (ex: 'France' au lieu d'une ville)\n"
            + "- Simplifier les mots-clés\n"
            + "- Changer le type de contrat\n\n"
            + f"🔍 [Voir sur HelloWork]({search_link})"
        )

    parts = [header, f"**{len(jobs)} offre(s) trouvée(s)**\n"]
    for i, job in enumerate(jobs, 1):
        parts.append(f"---\n### {i}. {job.to_markdown()}\n")

    search_link = _build_search_link(keywords, location)
    parts.append(f"\n🔍 [Voir toutes les offres sur HelloWork]({search_link})")

    return "\n".join(parts)


@mcp.tool()
async def get_job_details(job_url: str) -> str:
    """
    Récupère la description complète d'une offre HelloWork.

    Args:
        job_url: URL complète de l'offre (ex: https://www.hellowork.com/fr-fr/emplois/12345678.html)

    Returns:
        Description complète et critères détaillés de l'offre
    """
    if "hellowork.com" not in job_url:
        return "URL invalide. Fournissez une URL HelloWork valide (ex: https://www.hellowork.com/fr-fr/emplois/12345678.html)"

    details = await scraper.get_job_details(job_url)

    if details["description"] == "Non disponible":
        return (
            "HelloWork n'a pas répondu dans les délais ou la page est inaccessible.\n"
            f"Vous pouvez consulter l'offre directement : {job_url}"
        )

    lines = [f"## Détail de l'offre\n🔗 {job_url}\n"]

    if details.get("criteria"):
        lines.append("### Critères")
        for key, value in details["criteria"].items():
            lines.append(f"- **{key.capitalize()} :** {value}")
        lines.append("")

    lines.append("### Description complète")
    lines.append(details["description"])

    return "\n".join(lines)


@mcp.tool()
async def search_and_analyze(
    keywords: str,
    location: str = "France",
    contract_type: str = "all",
    remote: str = "all",
    user_profile: str = "",
) -> str:
    """
    Recherche des offres et analyse leur correspondance avec un profil.

    Args:
        keywords: Mots-clés de recherche
        location: Localisation
        contract_type: Type de contrat
        remote: Mode de travail
        user_profile: Description du profil/expériences de l'utilisateur
                      (ex: "10 ans d'expérience en transport routier, permis C, management d'équipe")
                      Si fourni, chaque offre est scorée en correspondance.

    Returns:
        Offres trouvées, classées par pertinence si profil fourni
    """
    jobs = await scraper.search(
        keywords=keywords,
        location=location,
        contract_type=contract_type,
        remote=remote,
        limit=15,
    )

    if not jobs:
        return (
            "Aucune offre trouvée sur HelloWork pour ces critères.\n\n"
            "**Suggestions :** élargir la localisation, modifier les mots-clés, changer le type de contrat."
        )

    header = f"## Analyse HelloWork — {keywords} | {location}\n\n"

    if not user_profile:
        parts = [header, f"**{len(jobs)} offre(s) trouvée(s)** (aucun profil fourni — tri par défaut)\n"]
        for i, job in enumerate(jobs, 1):
            parts.append(f"---\n### {i}. {job.to_markdown()}\n")
        return "\n".join(parts)

    # Scoring simple par correspondance de mots-clés du profil
    profile_words = set(user_profile.lower().split())

    def score(job) -> int:
        text = (
            f"{job.title} {job.company} {job.description_snippet} {job.contract_type} {job.remote_type}"
        ).lower()
        return sum(1 for word in profile_words if len(word) > 3 and word in text)

    ranked = sorted(jobs, key=score, reverse=True)

    parts = [
        header,
        f"**Profil analysé :** _{user_profile}_\n",
        f"**{len(ranked)} offre(s) classée(s) par pertinence**\n",
    ]
    for i, job in enumerate(ranked, 1):
        match_score = score(job)
        badge = "🟢" if match_score >= 3 else "🟡" if match_score >= 1 else "🔴"
        parts.append(f"---\n### {i}. {badge} (score: {match_score})\n{job.to_markdown()}\n")

    return "\n".join(parts)


def main() -> None:
    port = int(os.environ.get("PORT", 8000))
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=port,
        path="/mcp",
    )


if __name__ == "__main__":
    main()
