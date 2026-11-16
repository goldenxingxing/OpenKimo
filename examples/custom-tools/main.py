import asyncio
from pathlib import Path

from kaos.path import KaosPath

from kimo_cli.app import KimoCLI, enable_logging
from kimo_cli.session import Session


async def main():
    enable_logging()
    session = await Session.create(KaosPath.cwd())
    myagent = Path(__file__).parent / "myagent.yaml"
    instance = await KimoCLI.create(session, agent_file=myagent)
    await instance.run_print(
        input_format="text",
        output_format="text",
        command="What tools do you have?",
    )


if __name__ == "__main__":
    asyncio.run(main())
