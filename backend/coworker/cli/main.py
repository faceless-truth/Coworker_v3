import uuid

import click
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession


@click.group()
def cli() -> None:
    """MC & S CoWorker v3 CLI."""
    pass


@cli.command()
def version() -> None:
    """Print the current version."""
    click.echo("MC & S CoWorker v3.0.0")

@cli.command("create-firm")
@click.argument("name")
@click.option("--slug", default=None, help="URL-safe identifier. Defaults to slugify(name).")
@click.option("--abn", default=None, help="Australian Business Number, exactly 11 digits.")
@click.option("--timezone", "timezone_", default="Australia/Melbourne", show_default=True,
              help="IANA timezone name (e.g. Australia/Sydney).")
def create_firm(name: str, slug: str | None, abn: str | None, timezone_: str) -> None:
    """Create a new firm tenant."""
    import asyncio
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    from slugify import slugify
    from coworker.db.session import SessionLocal, firm_context
    from coworker.db.models.tenancy import Firm

    if abn is not None and (len(abn) != 11 or not abn.isdigit()):
        raise click.BadParameter("ABN must be exactly 11 digits.", param_hint="--abn")

    try:
        ZoneInfo(timezone_)
    except ZoneInfoNotFoundError as exc:
        raise click.BadParameter(f"Unknown IANA timezone: {timezone_!r}.", param_hint="--timezone") from exc

    resolved_slug = slug if slug is not None else slugify(name)

    # Pre-generate the firm id so we can enter firm_context BEFORE the
    # INSERT. Under FORCE ROW LEVEL SECURITY on `firms` (Stage C2), the
    # INSERT's WITH CHECK predicate is `id = NULLIF(current_setting('app.firm_id',
    # true), '')::uuid`, so app.firm_id must already match the row's id at
    # transaction begin or the INSERT is denied. The Session after_begin
    # listener picks the value up from the firm_context ContextVar.
    firm_id = uuid.uuid4()

    async def _create():
        async with SessionLocal() as session, firm_context(firm_id):
            firm = Firm(id=firm_id, name=name, slug=resolved_slug, abn=abn, timezone=timezone_)
            session.add(firm)
            await session.commit()
        click.echo(f"Created firm '{name}' with slug '{resolved_slug}' (id={firm_id})")

    asyncio.run(_create())


@cli.command("bootstrap-firm")
@click.option("--slug", required=True, help="URL-safe identifier. UPSERT key.")
@click.option("--name", required=True, help="Display name for the firm.")
@click.option("--azure-tenant-id", "azure_tenant_id", required=True,
              help="GUID of the firm's Microsoft 365 tenant.")
@click.option("--azure-client-id", "azure_client_id", required=True,
              help="GUID of the firm's Azure AD app registration (client ID).")
@click.option("--azure-client-secret", "azure_client_secret", required=True,
              help="Client secret for the Azure AD app. Encrypted before storage.")
@click.option("--timezone", "timezone_", default="Australia/Melbourne", show_default=True,
              help="IANA timezone name (e.g. Australia/Sydney).")
@click.option("--abn", default=None, help="Australian Business Number, exactly 11 digits.")
def bootstrap_firm(
    slug: str,
    name: str,
    azure_tenant_id: str,
    azure_client_id: str,
    azure_client_secret: str,
    timezone_: str,
    abn: str | None,
) -> None:
    """Provision (or refresh) a firm with encrypted Azure AD credentials.

    Idempotent on --slug. On a fresh slug, all fields are persisted. On an
    existing slug, only the three Azure credential fields are refreshed —
    name/abn/timezone are left as-is. To change those, edit the row directly.
    """
    import asyncio

    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from coworker.db.session import SessionLocal

    try:
        uuid.UUID(azure_tenant_id)
    except ValueError as exc:
        raise click.BadParameter(
            f"Not a valid GUID: {azure_tenant_id!r}.", param_hint="--azure-tenant-id"
        ) from exc
    try:
        uuid.UUID(azure_client_id)
    except ValueError as exc:
        raise click.BadParameter(
            f"Not a valid GUID: {azure_client_id!r}.", param_hint="--azure-client-id"
        ) from exc

    if abn is not None and (len(abn) != 11 or not abn.isdigit()):
        raise click.BadParameter("ABN must be exactly 11 digits.", param_hint="--abn")

    try:
        ZoneInfo(timezone_)
    except ZoneInfoNotFoundError as exc:
        raise click.BadParameter(
            f"Unknown IANA timezone: {timezone_!r}.", param_hint="--timezone"
        ) from exc

    async def _run() -> uuid.UUID:
        async with SessionLocal() as session:
            firm_id = await _bootstrap_firm(
                session,
                slug=slug,
                name=name,
                azure_tenant_id=azure_tenant_id,
                azure_client_id=azure_client_id,
                azure_client_secret=azure_client_secret,
                timezone=timezone_,
                abn=abn,
            )
            await session.commit()
            return firm_id

    firm_id = asyncio.run(_run())
    click.echo(str(firm_id))


async def _bootstrap_firm(
    session: AsyncSession,
    *,
    slug: str,
    name: str,
    azure_tenant_id: str,
    azure_client_id: str,
    azure_client_secret: str,
    timezone: str = "Australia/Melbourne",
    abn: str | None = None,
) -> uuid.UUID:
    """UPSERT a firm by slug. Returns the firm's UUID. Caller commits.

    The lookup uses `lookup_firm_by_slug`, which lifts FORCE RLS on
    `firms` only for its SELECT. We then commit (closing that
    transaction) and do the INSERT/UPDATE in a fresh transaction under
    `firm_context(firm_id)` — the per-row INSERT/UPDATE policies pass
    because app.firm_id matches the row's id. This keeps RLS bypass
    narrowly scoped to the slug-to-id resolution step.
    """
    from coworker.db.firms import lookup_firm_by_slug
    from coworker.db.models.tenancy import Firm
    from coworker.db.session import firm_context
    from coworker.security.encryption import encrypt_str

    existing = await lookup_firm_by_slug(session, slug)
    is_new = existing is None
    firm_id = uuid.uuid4() if is_new else existing.id  # type: ignore[union-attr]
    ciphertext = encrypt_str(azure_client_secret, firm_id=str(firm_id))

    # Close the lookup's transaction so the next execute (INSERT/UPDATE)
    # opens a fresh one under firm_context — that's the only way the
    # after_begin listener can apply app.firm_id for the per-row policy.
    await session.commit()

    async with firm_context(firm_id):
        if is_new:
            session.add(
                Firm(
                    id=firm_id,
                    slug=slug,
                    name=name,
                    abn=abn,
                    timezone=timezone,
                    azure_tenant_id=azure_tenant_id,
                    azure_client_id=azure_client_id,
                    azure_client_secret_ciphertext=ciphertext,
                )
            )
        else:
            await session.execute(
                update(Firm)
                .where(Firm.id == firm_id)
                .values(
                    azure_tenant_id=azure_tenant_id,
                    azure_client_id=azure_client_id,
                    azure_client_secret_ciphertext=ciphertext,
                )
            )
        await session.flush()
    return firm_id
