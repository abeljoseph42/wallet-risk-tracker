"""Load OFAC-sanctioned Ethereum addresses into address_labels (idempotent sync).

Usage (from backend/):
    python scripts/ingest_ofac.py                 # download the current official SDN.XML
    python scripts/ingest_ofac.py --file SDN.XML  # use a local copy
"""

import argparse
import asyncio
from pathlib import Path

import httpx

from app.db.session import get_engine, get_sessionmaker
from app.services.labels import OFAC_SDN_XML_URL, download_sdn_xml, parse_sdn_xml, sync_labels


async def main(local_xml: tuple[bytes, str] | None) -> None:
    if local_xml is not None:
        xml_bytes, origin = local_xml
    else:
        async with httpx.AsyncClient() as http:
            xml_bytes, origin = await download_sdn_xml(http), OFAC_SDN_XML_URL

    parsed = parse_sdn_xml(xml_bytes)
    print(f"Source:        {origin}")
    print(f"Publish date:  {parsed.publish_date}")
    print(f"ETH addresses: {len(parsed.labels)}")

    async with get_sessionmaker()() as session:
        counts = await sync_labels(session, parsed.labels)
    await get_engine().dispose()
    print(
        f"Inserted {counts.inserted}, updated {counts.updated}, "
        f"unchanged {counts.unchanged}, removed {counts.removed} (total {counts.total})"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, help="local SDN.XML instead of downloading")
    file = parser.parse_args().file
    asyncio.run(main((file.read_bytes(), str(file)) if file else None))
