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
#
# Sélecteurs CSS confirmés par inspection du HTML réel (mai 2025) :
#   Card    : [data-cy="serpCard"]
#   Titre   : [data-cy="offerTitle"]   (balise <a>)
#   Ville   : [data-cy="localisationCard"]
#   Contrat : [data-cy="contractCard"]
#   Tags    : [data-cy="contractTag"]  (télétravail, durée…)
#   Salaire : div.tw-typo-s-bold       (sans data-cy, contient "€")
#   Date    : div.tw-typo-s.tw-text-grey-500
#   Société : p.tw-typo-s.tw-inline


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

    # Mots-clés télétravail dans les tags
    _REMOTE_MAP = {
        "télétravail total": "Télétravail",
        "full remote": "Télétravail",
        "télétravail partiel": "Hybride",
        "télétravail occasionnel": "Hybride",
        "télétravail": "Télétravail",
        "hybride": "Hybride",
        "hybrid": "Hybride",
        "présentiel": "Sur site",
        "sur site": "Sur site",
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

    # ------------------------------------------------------------------ #
    #  Construction URL                                                    #
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    #  Requête HTTP                                                        #
    # ------------------------------------------------------------------ #

    async def _fetch(self, url: str) -> str | None:
        """Requête GET avec retry unique sur 429."""
        try:
            response = await self._client.get(url)
            if response.status_code == 429:
                logger.warning("Rate limited (429) — attente 2s puis retry")
                await asyncio.sleep(2)
                response = await self._client.get(url)
            response.raise_for_status()
            return response.text
        except httpx.TimeoutException:
            logger.error("Timeout : %s", url)
        except httpx.HTTPStatusError as exc:
            logger.error("HTTP %s : %s", exc.response.status_code, url)
        except httpx.RequestError as exc:
            logger.error("Erreur réseau : %s", exc)
        return None

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _extract_id_from_url(self, url: str) -> str:
        match = re.search(r"/(\d+)\.html", url)
        return match.group(1) if match else url.split("/")[-1]

    def _absolute_url(self, href: str) -> str:
        return href if href.startswith("http") else "https://www.hellowork.com" + href

    # ------------------------------------------------------------------ #
    #  Parsing des cards                                                   #
    # ------------------------------------------------------------------ #

    def _parse_cards(self, soup: BeautifulSoup) -> list:
        """
        Sélectionne les cards d'offres avec plusieurs stratégies de fallback.
        Stratégie 1 (data-cy="serpCard") confirmée sur le HTML réel de mai 2025.
        """
        # Stratégie 1 : sélecteur principal confirmé
        cards = soup.select('[data-cy="serpCard"]')
        if cards:
            logger.debug("Sélecteur [data-cy=serpCard] → %d cards", len(cards))
            return cards

        # Stratégie 2 : parents directs des liens d'offres
        seen: set[int] = set()
        fallback = []
        for a in soup.select('a[data-cy="offerTitle"]'):
            parent = a.find_parent("li") or a.find_parent("article") or a.find_parent("div")
            if parent and id(parent) not in seen:
                seen.add(id(parent))
                fallback.append(parent)
        if fallback:
            logger.debug("Sélecteur fallback offerTitle-parent → %d cards", len(fallback))
            return fallback

        # Stratégie 3 : tous liens /emplois/ → remonte au container
        seen2: set[int] = set()
        fallback2 = []
        for a in soup.select('a[href*="/emplois/"]'):
            parent = a.find_parent("li") or a.find_parent("article") or a.find_parent("div")
            if parent and id(parent) not in seen2:
                seen2.add(id(parent))
                fallback2.append(parent)
        if fallback2:
            logger.debug("Sélecteur fallback /emplois/ → %d cards", len(fallback2))
            return fallback2

        logger.warning("Aucun sélecteur n'a trouvé de cards — structure HelloWork modifiée ?")
        return []

    def _extract_job_from_card(self, card) -> HelloWorkJob | None:
        try:
            # --- TITRE & URL ---
            title_tag = card.select_one('[data-cy="offerTitle"]')
            if not title_tag:
                title_tag = card.select_one('a[href*="/emplois/"]')
            if not title_tag:
                return None

            title = title_tag.get_text(strip=True)
            href = title_tag.get("href", "")
            job_url = self._absolute_url(href) if href else ""
            job_id = self._extract_id_from_url(href) if href else ""

            # --- ENTREPRISE ---
            # p.tw-typo-s.tw-inline contient le nom de l'entreprise
            company = "Non précisé"
            company_tag = card.select_one("p.tw-typo-s.tw-inline")
            if not company_tag:
                # fallback : premier <p> différent du titre
                for p in card.select("p"):
                    txt = p.get_text(strip=True)
                    if txt and txt != title and len(txt) < 80:
                        company = txt
                        break
            else:
                company = company_tag.get_text(strip=True)

            # --- LOCALISATION ---
            location = "Non précisé"
            loc_tag = card.select_one('[data-cy="localisationCard"]')
            if loc_tag:
                location = loc_tag.get_text(strip=True)

            # --- TYPE DE CONTRAT ---
            contract_type = "Non précisé"
            ct_tag = card.select_one('[data-cy="contractCard"]')
            if ct_tag:
                contract_type = ct_tag.get_text(strip=True)

            # --- SALAIRE ---
            # Le salaire est dans un div.tw-typo-s-bold sans data-cy
            # Il contient "€" — on filtre pour éviter les faux positifs
            salary = "Non précisé"
            for el in card.select("div"):
                classes = " ".join(el.get("class", []))
                if "tw-typo-s-bold" in classes and not el.get("data-cy"):
                    text = el.get_text(strip=True)
                    if "€" in text and len(text) < 80:
                        # Nettoie l'espace insécable Unicode
                        salary = text.replace(" ", " ").strip()
                        break

            # --- MODE TÉLÉTRAVAIL ---
            # data-cy="contractTag" contient "Télétravail partiel", "Télétravail occasionnel", etc.
            remote_type = "Non précisé"
            for tag_el in card.select('[data-cy="contractTag"]'):
                text_lower = tag_el.get_text(strip=True).lower()
                for kw, label in self._REMOTE_MAP.items():
                    if kw in text_lower:
                        remote_type = label
                        break
                if remote_type != "Non précisé":
                    break

            # --- DATE DE PUBLICATION ---
            # div.tw-typo-s.tw-text-grey-500 contient "il y a X heures/jours"
            posted_at = ""
            date_tag = card.select_one("div.tw-typo-s.tw-text-grey-500")
            if date_tag:
                posted_at = date_tag.get_text(strip=True)
            else:
                # Fallback : cherche n'importe quel élément avec "il y a"
                for el in card.select("span, p, div"):
                    text = el.get_text(strip=True).lower()
                    if ("il y a" in text or "aujourd" in text or "hier" in text) and len(text) < 40:
                        posted_at = el.get_text(strip=True)
                        break

            return HelloWorkJob(
                id=job_id,
                title=title,
                company=company,
                location=location,
                contract_type=contract_type,
                remote_type=remote_type,
                salary=salary,
                posted_at=posted_at,
                description_snippet="",
                url=job_url,
            )
        except Exception as exc:
            logger.warning("Erreur extraction card : %s", exc)
            return None

    # ------------------------------------------------------------------ #
    #  API publique                                                        #
    # ------------------------------------------------------------------ #

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

        logger.info("%d offres extraites sur %d cards", len(jobs), len(cards))
        return jobs

    async def get_job_details(self, job_url: str) -> dict:
        """
        Récupère la description complète d'une offre individuelle.

        Sélecteurs confirmés (mai 2025) :
          - div.tw-typo-long-m       → container principal de la description
          - p.tw-do-html-p-margin    → paragraphes individuels de la description

        Note : HelloWork peut bloquer les IPs de datacenter (Render, etc.)
        sur les pages /emplois/ tout en laissant passer la page de recherche.
        En cas d'échec le caller reçoit accessible=False + l'URL directe.
        """
        # Timeout court : fail fast plutôt qu'attendre 15s sur Render
        html = await self._fetch_detail(job_url)
        if html is None:
            return {
                "description": None,
                "criteria": {},
                "accessible": False,
                "url": job_url,
            }

        soup = BeautifulSoup(html, "lxml")

        # --- Description complète ---
        # Stratégie 1 : container tw-typo-long-m (confirmé mai 2025)
        description = ""
        desc_container = soup.select_one("div.tw-typo-long-m")
        if desc_container:
            paragraphs = desc_container.select("p.tw-do-html-p-margin")
            if paragraphs:
                description = "\n\n".join(
                    p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True)
                )
                logger.debug("Description via div.tw-typo-long-m (%d §)", len(paragraphs))

        # Stratégie 2 : tous les p.tw-do-html-p-margin de la page
        if not description:
            paragraphs = soup.select("p.tw-do-html-p-margin")
            if paragraphs:
                description = "\n\n".join(
                    p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True)
                )
                logger.debug("Description via p.tw-do-html-p-margin (%d §)", len(paragraphs))

        # Stratégie 3 : anciens sélecteurs (fallback si structure change)
        if not description:
            for selector in ["div[class*='tw-prose']", "div[class*='job-description']", "section[class*='description']"]:
                tag = soup.select_one(selector)
                if tag:
                    description = tag.get_text(separator="\n", strip=True)
                    logger.debug("Description via fallback : %s", selector)
                    break

        description = description[:2000] if description else ""

        # --- Critères supplémentaires ---
        criteria: dict[str, str] = {}
        # Salaire affiché sur la fiche (data-cy="salary-tag-button")
        sal_tag = soup.select_one('[data-cy="salary-tag-button"]')
        if sal_tag:
            criteria["salaire"] = sal_tag.get_text(strip=True).replace(" ", " ")

        # Titre du poste confirmé
        job_title_tag = soup.select_one('[data-cy="jobTitle"]')
        if job_title_tag:
            criteria["titre"] = job_title_tag.get_text(strip=True)

        return {
            "description": description or "Description non disponible dans le HTML.",
            "criteria": criteria,
            "accessible": True,
            "url": job_url,
        }

    async def _fetch_detail(self, url: str) -> str | None:
        """Requête vers une page de détail avec timeout réduit (8s)."""
        try:
            response = await self._client.get(url, timeout=8.0)
            if response.status_code == 429:
                logger.warning("Rate limited (429) sur fiche — attente 2s")
                await asyncio.sleep(2)
                response = await self._client.get(url, timeout=8.0)
            if response.status_code in (403, 404, 410):
                logger.warning("Accès refusé %s : %s", response.status_code, url)
                return None
            response.raise_for_status()
            return response.text
        except httpx.TimeoutException:
            logger.warning("Timeout fiche (8s) : %s — IP peut-être bloquée par HelloWork", url)
        except httpx.HTTPStatusError as exc:
            logger.error("HTTP %s fiche : %s", exc.response.status_code, url)
        except httpx.RequestError as exc:
            logger.error("Erreur réseau fiche : %s", exc)
        return None

    async def close(self) -> None:
        await self._client.aclose()
