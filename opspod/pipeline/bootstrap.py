"""Wire real dependencies from settings; shared by the API and the worker."""

from pathlib import Path

from opspod.core.config import LLMMode, Settings
from opspod.db.flags import DbChaos
from opspod.db.session import make_sessionmaker
from opspod.evals.stubs import STUBS
from opspod.llm.config import load_llm_config
from opspod.llm.factory import build_gateway
from opspod.llm.providers.fake import FakeProvider
from opspod.observability.metrics import llm_event_sink
from opspod.pipeline.deps import Deps
from opspod.policy.loader import load_all
from opspod.skills import STATIC_SKILLS
from opspod.skills.fetch_trip_context import FixtureRepo, make_skill
from opspod.tools.registry import default_registry


def build_deps(settings: Settings, *, record_cassettes: bool = False) -> Deps:
    sessionmaker = make_sessionmaker(settings.database_url)
    # Chaos flags are honoured only in dev, and read from the DB so API and worker agree.
    chaos = DbChaos(sessionmaker) if settings.app_env == "dev" else None
    fake = FakeProvider(STUBS["good"]) if settings.llm_mode is LLMMode.FAKE else None
    return Deps(
        sessionmaker=sessionmaker,
        gateway=build_gateway(
            settings.llm_mode,
            load_llm_config(Path(settings.llm_config))
            if settings.llm_config
            else load_llm_config(),
            settings,
            fake=fake,
            record_cassettes=record_cassettes,
            on_event=llm_event_sink,
            chaos=chaos,
        ),
        policies=load_all(),
        fetch_context=make_skill(FixtureRepo.from_datasets()),
        tools=default_registry(chaos),
        skills=STATIC_SKILLS,
        chaos=chaos,
    )
