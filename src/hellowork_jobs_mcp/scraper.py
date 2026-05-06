import asyncio
import logging
import re
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup
from fake_useragent import UserAgent

from .models import HelloWorkJob

logger = logging.getLogger(__name__)

# HelloWork est le premier jobboard français : il affiche souvent les salaires
# dans les cards publiques, contrairement à LinkedIn qui les masque aux non-connectés.
# Il couvre aussi très bien les PME françaises et les contrats spécifiques
# (alternance, stage, intérim) comme filtres natifs.


class HelloWorkScraper:
    BASE_URL = "https://www.hellowork.com/fr-fr/emploi/recherche.html"

    # Mapping interne → paramètre URL "d"
    CONTRACT_TYPES: dict[str, str] = {
        "all": "all",
        "cdi": "CDI",
        "cdd": "CDD",
        "alternance": "alternance",
        "stage": "stage",
        "interim": "interim",
        "independant": "freelance",
    }

    # Mapping interne → paramètre URL "et"
    REMOTE_TYPES: dict[str, str | None] = {
        "all": None,      # ne pas inclure le paramètre
        "remote": "2",
        "hybrid": "3",
        "on_site": "1",
    }

    def __init__(self) -> None:
        ua = UserAgent()
        self._client = httpx.AsyncClient(
            timeout=15.0,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "User-Agent": ua.random,
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
            follow_redirects=True,
        )

    def _build_url(
        self,
        keywords: str,
        location: str,
        contract_type: str,
        remote: str,
        page: int = 1,
    ) -> str:
        params: dict[str, str] = {
            "k": keywords,
            "l": location,
            "ray": "all",
            "p": str(page),
        }

        d_value = self.CONTRACT_TYPES.get(contract_type, "all")
        if d_value != "all":
            params["d"] = d_value

        et_value = self.REMOTE_TYPES.get(remote)
        if et_value is not None:
            params["et"] = et_value

        return f"{self.BASE_URL}?{urlencode(params)}"

    async def _fetch(self, url: str) -> str | None:
        """Effectue la requête HTTP avec une seule tentative de retry sur 429."""
        try:
            response = await self._client.get(url)
            if response.status_code == 429:
                logger.warning("Rate limited (429) — attente 2s puis retry")
                await asyncio.sleep(2)
                response = await self._client.get(url)
            response.raise_for_status()
            return response.text
        except httpx.TimeoutException:
            logger.error("Timeout lors de la requête : %s", url)
        except httpx.HTTPStatusError as exc:
            logger.error("Erreur HTTP %s pour : %s", exc.response.status_code, url)
        except httpx.RequestError as exc:
            logger.error("Erreur réseau : %s", exc)
        return None

    def _extract_id_from_url(self, url: str) -> str:
        # Ex: /fr-fr/emplois/52202814.html → "52202814"
        match = re.search(r"/(\d+)\.html", url)
        return match.group(1) if match else url.split("/")[-1]

    def _absolute_url(self, href: str) -> str:
        if href.startswith("http"):
            return href
        return "https://www.hellowork.com" + href

    def _parse_cards(self, soup: BeautifulSoup) -> list:
        """
        Tente plusieurs stratégies de sélection pour résister aux changements
        de classes TailwindCSS de HelloWork.
        """
        # Stratégie 1 : attribut data-cy (le plus stable)
        cards = soup.select('[data-cy="jobCard"]')
        if cards:
            logger.debug("Sélecteur retenu : [data-cy=jobCard] (%d cards)", len(cards))
            return cards

        # Stratégie 2 : balises <article> avec classe tw-group (Tailwind)
        cards = soup.select("article.tw-group, li.tw-group")
        if cards:
            logger.debug("Sélecteur retenu : article/li.tw-group (%d cards)", len(cards))
            return cards

        # Stratégie 3 : articles génériques
        cards = soup.select("article[data-type='job']")
        if cards:
            logger.debug("Sélecteur retenu : article[data-type=job] (%d cards)", len(cards))
            return cards

        # Stratégie 4 : classes contenant "JobCard"
        cards = soup.select("div[class*='JobCard'], li[class*='job-item']")
        if cards:
            logger.debug("Sélecteur retenu : JobCard/job-item (%d cards)", len(cards))
            return cards

        # Stratégie 5 : tout article contenant un lien d'offre
        cards = [
            a.find_parent("article") or a.find_parent("li")
            for a in soup.select("a[href*='/emplois/']")
            if a.find_parent("article") or a.find_parent("li")
        ]
        cards = list({id(c): c for c in cards if c}.values())
        if cards:
            logger.debug("Sélecteur retenu : parents de liens /emplois/ (%d cards)", len(cards))
            return cards

        logger.warning("Aucun sélecteur n'a trouvé de cards — structure HTML peut-être modifiée")
        return []

    def _extract_job_from_card(self, card) -> HelloWorkJob | None:
        try:
            # --- TITRE & URL ---
            # Priorité 1 : data-cy
            title_tag = card.select_one('[data-cy="jobTitle"]')
            # Priorité 2 : lien avec classe tw-leading (Tailwind)
            if not title_tag:
                title_tag = card.select_one("a[class*='tw-leading']")
            # Priorité 3 : premier <a> vers /emplois/
            if not title_tag:
                title_tag = card.select_one("a[href*='/emplois/']")
            # Priorité 4 : h3
            if not title_tag:
                title_tag = card.select_one("h3")

            if not title_tag:
                return None

            title = title_tag.get_text(strip=True)
            href = title_tag.get("href", "")
            if not href and title_tag.name != "a":
                a_tag = title_tag.find("a")
                href = a_tag.get("href", "") if a_tag else ""
            job_url = self._absolute_url(href) if href else ""
            job_id = self._extract_id_from_url(href) if href else ""

            # --- ENTREPRISE ---
            company = "Non précisé"
            company_tag = card.select_one('[data-cy="company"]')
            if not company_tag:
                # span avec tw-mr-2 ou classe similaire
                company_tag = card.select_one("span[class*='tw-mr-2']")
            if not company_tag:
                # cherche un <p> ou <span> qui suit le titre
                for tag in card.select("p, span"):
                    text = tag.get_text(strip=True)
                    if text and text != title and len(text) < 80:
                        company = text
                        break
            if company_tag:
                company = company_tag.get_text(strip=True)

            # --- LOCALISATION & SALAIRE ---
            # tw-text-tertiary est réutilisé pour les deux — on prend dans l'ordre
            tertiary_tags = card.select("span[class*='tw-text-tertiary'], [data-cy='location'], [data-cy='salary']")
            location = "Non précisé"
            salary = "Non précisé"

            # On peut aussi chercher spécifiquement par data-cy
            loc_tag = card.select_one('[data-cy="location"]')
            sal_tag = card.select_one('[data-cy="salary"]')

            if loc_tag:
                location = loc_tag.get_text(strip=True)
            if sal_tag:
                salary = sal_tag.get_text(strip=True)

            if not loc_tag and tertiary_tags:
                location = tertiary_tags[0].get_text(strip=True)
                if len(tertiary_tags) > 1:
                    salary_text = tertiary_tags[1].get_text(strip=True)
                    # Heuristique : le salaire contient souvent "€", "K€", "k€", "an", "mois"
                    if any(kw in salary_text for kw in ["€", "k€", "K€", "an", "mois", "brut", "net"]):
                        salary = salary_text

            # --- TYPE DE CONTRAT ---
            contract_type = "Non précisé"
            # Cherche dans les badges/tags de la card
            contract_keywords = ["CDI", "CDD", "Alternance", "Stage", "Intérim", "Freelance", "Indépendant", "Interim"]
            for tag in card.select("span, div, li"):
                text = tag.get_text(strip=True)
                if text in contract_keywords:
                    contract_type = text
                    break
            # Fallback : data-cy
            if contract_type == "Non précisé":
                ct_tag = card.select_one('[data-cy="contract"]')
                if ct_tag:
                    contract_type = ct_tag.get_text(strip=True)

            # --- MODE TÉLÉTRAVAIL ---
            remote_type = "Non précisé"
            remote_keywords = {
                "télétravail total": "Télétravail",
                "télétravail": "Télétravail",
                "full remote": "Télétravail",
                "hybride": "Hybride",
                "hybrid": "Hybride",
                "présentiel": "Sur site",
                "sur site": "Sur site",
                "sur place": "Sur site",
            }
            for tag in card.select("span, div, li, p"):
                text = tag.get_text(strip=True).lower()
                for kw, label in remote_keywords.items():
                    if kw in text:
                        remote_type = label
                        break
                if remote_type != "Non précisé":
                    break

            # --- DATE DE PUBLICATION ---
            posted_at = ""
            time_tag = card.select_one("time")
            if time_tag:
                posted_at = time_tag.get("datetime", "") or time_tag.get_text(strip=True)
            else:
                date_kw = ["il y a", "aujourd'hui", "hier", "semaine", "mois"]
                for tag in card.select("span, p, div"):
                    text = tag.get_text(strip=True).lower()
                    if any(kw in text for kw in date_kw) and len(text) < 40:
                        posted_at = tag.get_text(strip=True)
                        break

            # --- DESCRIPTION SNIPPET ---
            snippet = ""
            desc_tag = card.select_one('[data-cy="jobDescription"], p[class*="tw-text"]')
            if desc_tag:
                snippet = desc_tag.get_text(strip=True)[:300]

            return HelloWorkJob(
                id=job_id,
                title=title,
                company=company,
                location=location,
                contract_type=contract_type,
                remote_type=remote_type,
                salary=salary,
                posted_at=posted_at,
                description_snippet=snippet,
                url=job_url,
            )
        except Exception as exc:
            logger.warning("Erreur extraction card : %s", exc)
            return None

    async def search(
        self,
        keywords: str,
        location: str = "France",
        contract_type: str = "all",
        remote: str = "all",
        limit: int = 10,
    ) -> list[HelloWorkJob]:
        limit = min(limit, 25)
        url = self._build_url(keywords, location, contract_type, remote)
        logger.info("Recherche HelloWork : %s", url)

        html = await self._fetch(url)
        if not html:
            return []

        soup = BeautifulSoup(html, "lxml")
        cards = self._parse_cards(soup)

        jobs: list[HelloWorkJob] = []
        for card in cards[:limit]:
            job = self._extract_job_from_card(card)
            if job:
                jobs.append(job)

        logger.info("%d offres extraites", len(jobs))
        return jobs

    async def get_job_details(self, job_url: str) -> dict:
        """Récupère la description complète d'une offre individuelle."""
        html = await self._fetch(job_url)
        if not html:
            return {"description": "Non disponible", "criteria": {}}

        soup = BeautifulSoup(html, "lxml")

        # --- Description complète ---
        description = "Non disponible"
        desc_selectors = [
            '[data-cy="jobDescription"]',
            "div[class*='job-description']",
            "div[class*='tw-prose']",
            "section[class*='description']",
            "div[id*='description']",
        ]
        for selector in desc_selectors:
            tag = soup.select_one(selector)
            if tag:
                description = tag.get_text(separator=" ", strip=True)[:2000]
                logger.debug("Description extraite via : %s", selector)
                break

        # --- Critères détaillés ---
        criteria: dict[str, str] = {}
        criteria_keywords = {
            "expérience": ["expérience", "experience"],
            "études": ["études", "formation", "bac", "diplôme"],
            "secteur": ["secteur", "industrie"],
        }
        for label, kws in criteria_keywords.items():
            for tag in soup.select("li, span, div, td"):
                text = tag.get_text(strip=True).lower()
                if any(kw in text for kw in kws) and len(text) < 100:
                    criteria[label] = tag.get_text(strip=True)
                    break

        return {"description": description, "criteria": criteria}

    async def close(self) -> None:
        await self._client.aclose()
