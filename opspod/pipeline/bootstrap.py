"""Wire real dependencies from settings; shared by the API and the worker."""

from opspod.core.config import Settings
from opspod.db.session import make_sessionmaker
from opspod.llm.config import load_llm_config
from opspod.llm.factory import build_gateway
from opspod.pipeline.deps import Deps
from opspod.policy.loader import load_all
from opspod.skills import STATIC_SKILLS
from opspod.skills.fetch_trip_context import FixtureRepo, make_skill
from opspod.tools.registry import default_registry


def build_deps(settings: Settings, *, record_cassettes: bool = False) -> Deps:
    return Deps(
        sessionmaker=make_sessionmaker(settings.database_url),
        gateway=build_gateway(
            settings.llm_mode, load_llm_config(), settings, record_cassettes=record_cassettes
        ),
        policies=load_all(),
        fetch_context=make_skill(FixtureRepo.from_datasets()),
        tools=default_registry(),
        skills=STATIC_SKILLS,
    )
