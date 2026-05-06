from dataclasses import dataclass, field


@dataclass
class HelloWorkJob:
    id: str
    title: str
    company: str
    location: str
    contract_type: str
    remote_type: str
    salary: str
    posted_at: str
    description_snippet: str
    url: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "contract_type": self.contract_type,
            "remote_type": self.remote_type,
            "salary": self.salary,
            "posted_at": self.posted_at,
            "description_snippet": self.description_snippet,
            "url": self.url,
        }

    def to_markdown(self) -> str:
        lines = [
            f"💼 **{self.title}**",
            f"🏢 {self.company}",
            f"📍 {self.location}",
            f"📄 {self.contract_type}",
            f"🏠 {self.remote_type}",
        ]
        if self.salary and self.salary != "Non précisé":
            lines.append(f"💰 {self.salary}")
        if self.posted_at:
            lines.append(f"📅 {self.posted_at}")
        if self.description_snippet:
            lines.append(f"\n_{self.description_snippet}_")
        lines.append(f"\n🔗 {self.url}")
        return "\n".join(lines)
