from datetime import datetime
from datetime import timezone
import logging
import os
import pathlib

import dotenv
import lightbulb
import hikari
import miru
import saru
import typing as t


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logging.getLogger("lightbulb").setLevel(logging.DEBUG)


DEFAULT_BURG_CHANNEL_NAME = "burg"

dotenv.load_dotenv()


def get_dev_guilds() -> t.Sequence[int]:
    if "DEV_GUILDS" not in os.environ:
        logger.info("Running with global app commands.")
        return ()
    else:
        guilds = [int(s.strip()) for s in os.environ["DEV_GUILDS"].split(",")]
        logger.info(f"Running on debug guilds: {guilds}")
        return guilds


class BurgStatMixin:
    def count_burg(self: saru.GuildStateBase, count_key: str) -> None:
        if "burger_epoch" not in self.cfg:
            self.cfg.set("burger_epoch", datetime.utcnow().timestamp())

            self.cfg.set("burgers_posted", 0)
            self.cfg.set("angry_burgers_posted", 0)

        self.cfg.get_and_set(count_key, lambda x: x + 1)

    def num_burg(self: saru.GuildStateBase, count_key: str) -> int:
        if "burger_epoch" not in self.cfg:
            return 0

        return self.cfg.get(count_key)

    def average_burg_per_hour(self: saru.GuildStateBase, count_key: str) -> float:
        if "burger_epoch" not in self.cfg:
            return 0.0

        epoch = datetime.fromtimestamp(self.cfg.get("burger_epoch"), timezone.utc)
        now = datetime.fromtimestamp(datetime.utcnow().timestamp(), timezone.utc)
        delta = now - epoch

        # Take average over entire time burgers have been posted
        hours = delta.total_seconds() / (60*60)
        return round(self.cfg.get(count_key) / hours, 1)


# Yes, I'm abusing a config system to store data, don't @ me
@saru.config_backed("c/burg/stats")
class GlobalBurgStats(saru.GuildStateBase, BurgStatMixin):
    ...


@saru.config_backed("g/burg/stats")
class BurgStats(saru.GuildStateBase, BurgStatMixin):
    ...


@saru.config_backed("g/burg")
class BurgConfig(saru.GuildStateBase):
    def __init__(self, bot: lightbulb.BotApp, guild: hikari.Guild):
        super().__init__(bot, guild)
        self.stats: t.Optional[BurgStats] = None
        self.gstats: t.Optional[BurgStats] = None

    # bad
    async def ainit(self):
        s = saru.get(burgbot)
        self.stats: BurgStats = await s.gs(BurgStats, self.guild)
        self.gstats: GlobalBurgStats = await s.gs(GlobalBurgStats, self.guild)

    def is_channel_burg(self, channel: hikari.TextableGuildChannel) -> bool:
        return str(channel.id) in self.cfg.sub("channels")

    def has_any_channels(self) -> bool:
        return bool(self.cfg.get("channels"))

    async def create_burg_channel(self, guild_id: int, name: str) -> hikari.TextableGuildChannel:
        burg_permissions = [
            hikari.PermissionOverwrite(
                id=guild_id,
                type=hikari.PermissionOverwriteType.ROLE,
                deny=(
                    hikari.Permissions.SEND_MESSAGES |
                    hikari.Permissions.CREATE_PUBLIC_THREADS |
                    hikari.Permissions.CREATE_PRIVATE_THREADS
                )
            ),
            hikari.PermissionOverwrite(
                id=self.bot.get_me().id,
                type=hikari.PermissionOverwriteType.MEMBER,
                allow=(
                    hikari.Permissions.SEND_MESSAGES
                )
            )
        ]

        channel = await self.bot.rest.create_guild_text_channel(
            guild_id,
            name,
            permission_overwrites=burg_permissions
        )

        webhook = await self.bot.rest.create_webhook(channel, channel.name)

        cfg_obj = {
            "channel_id": channel.id,
            "webhook_id": webhook.id,
            "webhook_token": webhook.token,
            "button_id": None
        }

        self.cfg.sub("channels").set(str(channel.id), cfg_obj)
        return channel

    async def delete_burg_channel(self, ctx: lightbulb.Context, channel: hikari.TextableGuildChannel) -> None:
        path = f"channels/{channel.id}"

        burg_cfg = self.cfg.path_get(path)
        await ctx.bot.rest.delete_webhook(burg_cfg.get("webhook_id"))
        await ctx.bot.rest.delete_channel(burg_cfg.get("channel_id"))
        self.cfg.path_delete(path)

    async def remove_burg_button(self, message: hikari.Message) -> None:
        if str(message.channel_id) not in self.cfg.sub("channels"):
            return

        button_id = self.cfg.path_get(f"channels/{message.channel_id}/button_id")

        if button_id != message.id:
            logger.info(f"button_id {button_id} does not match message.id {message.id}")
            return

        await message.delete()

    async def create_burg_button(self, app: hikari.RESTAware, channel_id: int) -> None:
        channel = t.cast(hikari.TextableGuildChannel, await app.rest.fetch_channel(channel_id))
        burg_buttons = BurgView(self, channel_id)
        message = await channel.send(components=burg_buttons.build())
        self.cfg.path_set(f"channels/{channel.id}/button_id", message.id)
        burg_buttons.start(message)

    async def resume_views(self, app: hikari.RESTAware) -> None:
        for burg_cfg in self.cfg.get("channels").values():
            try:
                message = await app.rest.fetch_message(burg_cfg["channel_id"], burg_cfg["button_id"])
            except hikari.NotFoundError:
                # Burg buttons may have been deleted while the bot was offline. Recreate if so.
                await self.create_burg_button(app, burg_cfg["channel_id"])
                return

            BurgView(self, message.channel_id).start(message)

    async def post_to_burghook(
        self,
        app: hikari.RESTAware,
        channel_id: int,
        resource: hikari.Resourceish,
        avatar_url: hikari.URL,
        username: str
    ) -> None:
        webhook_id = self.cfg.path_get(f"channels/{channel_id}/webhook_id")
        webhook_token = self.cfg.path_get(f"channels/{channel_id}/webhook_token")
        await app.rest.execute_webhook(
            webhook_id,
            webhook_token,
            attachment=resource,
            avatar_url=avatar_url,
            username=username
        )


burgbot = lightbulb.BotApp(
    token=os.environ["BOT_TOKEN"],
    prefix=lightbulb.when_mentioned_or(["burg!"]),
    help_slash_command=True,
    case_insensitive_prefix_commands=True,
    default_enabled_guilds=get_dev_guilds(),
    intents=hikari.Intents.GUILD_MESSAGES | hikari.Intents.ALL_GUILDS_UNPRIVILEGED
)
saru.attach(
    burgbot,
    config_path=pathlib.Path("configdb"),
    cfgtemplate={
        "burg": {
            "channels": {}
        }
    }
)
s = saru.get(burgbot)
s.gstype(BurgConfig)
s.gstype(BurgStats)
s.gstype(GlobalBurgStats)
miru.load(burgbot)


@burgbot.listen(hikari.StartedEvent)
async def on_start(event: hikari.StartedEvent) -> None:
    for guild in burgbot.cache.get_guilds_view():
        cfg = t.cast(BurgConfig, await saru.get(burgbot).gs(BurgConfig, guild))
        await cfg.ainit()
        await cfg.resume_views(event.app)


def get_channel_by_name(ctx: lightbulb.Context, name: str) -> t.Optional[hikari.TextableGuildChannel]:
    for channel in ctx.get_guild().get_channels().values():
        if channel.name == name and channel.type == hikari.ChannelType.GUILD_TEXT:
            return t.cast(hikari.TextableGuildChannel, channel)

    return None


def error_embed(msg: str) -> hikari.Embed:
    embed = hikari.Embed(
        title="Error",
        color=hikari.Color.from_rgb(255, 0, 0),
        description=msg
    )

    return embed


def confirm_embed(msg: str) -> hikari.Embed:
    embed = hikari.Embed(
        title="Success",
        color=hikari.Color.from_rgb(0, 255, 0),
        description=msg
    )

    return embed


class BurgButton(miru.Button):
    """Press button, get burg."""
    def __init__(
        self,
        cfg: BurgConfig,
        burg: hikari.Resourceish,
        count_key: str,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.cfg = cfg
        self.burg = burg
        self.count_key = count_key

    async def callback(self, ctx: miru.Context) -> None:
        self.view.stop()
        await self.cfg.remove_burg_button(ctx.message)
        await self.cfg.post_to_burghook(
            ctx.app,
            ctx.channel_id,
            self.burg,
            ctx.member.display_avatar_url,
            ctx.member.display_name
        )
        self.cfg.gstats.count_burg(self.count_key)
        self.cfg.stats.count_burg(self.count_key)
        await self.cfg.create_burg_button(ctx.app, ctx.message.channel_id)


class BurgView(miru.View):
    def __init__(
        self,
        cfg: BurgConfig,
        channel_id: int
    ):
        super().__init__(timeout=None)
        self.add_item(BurgButton(
            cfg,
            label="burg",
            style=hikari.ButtonStyle.PRIMARY,
            burg=pathlib.Path("assets/burg.jpg"),
            count_key="burgers_posted",
            custom_id=f"burg{channel_id}"
        ))
        self.add_item(BurgButton(
            cfg,
            label="angry burg",
            style=hikari.ButtonStyle.DANGER,
            count_key="angry_burgers_posted",
            burg=pathlib.Path("assets/angryburg.jpg"),
            custom_id=f"angryburg{channel_id}"
        ))


@burgbot.command()
@lightbulb.add_checks(lightbulb.owner_only)
@lightbulb.command(
    "su",
    "Owner only maintenance commands."
)
@lightbulb.implements(lightbulb.PrefixCommandGroup)
async def su(*_) -> None: ...


@su.child()
@lightbulb.add_checks(lightbulb.owner_only)
@lightbulb.option(
    "type",
    "What type of app commands to purge. Can be \"guild\" or \"global\". Default is \"guild\".",
    default="guild"
)
@lightbulb.command(
    "reload-app-cmds",
    "Force reload all application commands."
)
@lightbulb.implements(lightbulb.PrefixSubCommand)
async def su_reload_app_cmds(ctx: lightbulb.Context) -> None:
    t: str = ctx.options.type.lower()
    if t not in ["guild", "global"]:
        await ctx.respond(error_embed(
            f"Bad command type \"{t}\", must be either \"guild\", \"global\"."
        ))
        return

    await saru.ack(ctx)

    if t == "guild":
        await ctx.bot.purge_application_commands(*get_dev_guilds())
    else:
        await ctx.bot.purge_application_commands(global_commands=True)

    await ctx.bot.sync_application_commands()

    await ctx.respond(confirm_embed(
        f"Successfully reloaded {t} application commands."
    ))


def populate_burg_embed(embed: hikari.Embed, cfg: BurgStatMixin, count_key: str, burgname: str) -> None:
    embed.add_field(
        f"{burgname}s posted all time",
        str(cfg.num_burg(count_key)),
    )
    embed.add_field(
        f"avg. {burgname}s per hour",
        str(cfg.average_burg_per_hour(count_key))
    )


@burgbot.command()
@lightbulb.option(
    "global",
    "Determines whether to get global stats or not.",
    type=bool,
    default=False
)
@lightbulb.command(
    "burg-stats",
    "Check your hot burger stats."
)
@lightbulb.implements(lightbulb.SlashCommand)
async def burg_stats(ctx: lightbulb.Context) -> None:
    embed = hikari.Embed()
    g = ctx.raw_options["global"]

    if g:
        embed.set_author(
            name=f"Global BurgerStats™️"
        )
        cfg = await GlobalBurgStats.get(ctx)
    else:
        embed.set_author(
            name=f"BurgerStats™️ for {ctx.get_guild().name}",
            icon=ctx.get_guild().icon_url
        )
        cfg = await BurgStats.get(ctx)

    populate_burg_embed(embed, cfg, "burgers_posted", "burg")
    populate_burg_embed(embed, cfg, "angry_burgers_posted", "angry burg")

    if not g:
        embed.set_footer("try /burg-stats global for global stats")

    await ctx.respond(embed)


@burgbot.command()
@lightbulb.command(
    "burg-channel",
    "Manage burg channels."
)
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def burg_channel(ctx: lightbulb.Context) -> None:
    pass


@burg_channel.child()
@lightbulb.add_checks(lightbulb.has_guild_permissions(
    hikari.Permissions.MANAGE_CHANNELS,
    hikari.Permissions.MANAGE_WEBHOOKS
))
@lightbulb.set_help(
    saru.longstr_oneline("""
        Create a burg channel. You can create as many burg channels as you want. Not sure why you'd want more
        than one, though.
    """)
)
@lightbulb.option(
    "name",
    f"The name of the channel to create. Defaults to \"{DEFAULT_BURG_CHANNEL_NAME}\".",
    default=DEFAULT_BURG_CHANNEL_NAME
)
@lightbulb.command(
    "create",
    "Create a burg channel."
)
@lightbulb.implements(lightbulb.SlashSubCommand)
async def burg_channel_create(ctx: lightbulb.Context) -> None:
    cfg = await BurgConfig.get(ctx)
    name: str = ctx.options.name

    channel = get_channel_by_name(ctx, name)
    if channel is not None:
        await ctx.respond(
            error_embed(
                f"Cannot create a burg channel with the name \"{name}\", since it already exists."
            ),
            flags=hikari.MessageFlag.EPHEMERAL
        )
        return

    channel = await cfg.create_burg_channel(ctx.guild_id, name)
    await ctx.respond(confirm_embed(
        f"New burg channel {channel.mention} created by {ctx.author.mention}."
    ))

    await cfg.create_burg_button(ctx.app, channel.id)


@burg_channel.child()
@lightbulb.add_checks(lightbulb.has_guild_permissions(
    hikari.Permissions.MANAGE_CHANNELS,
    hikari.Permissions.MANAGE_WEBHOOKS
))
@lightbulb.set_help(
    saru.longstr_oneline("""
        Delete a burg channel.
    """)
)
@lightbulb.option(
    "channel",
    "The channel to delete.",
    type=hikari.TextableGuildChannel
)
@lightbulb.command(
    "delete",
    "Delete a burg channel."
)
@lightbulb.implements(lightbulb.SlashSubCommand)
async def burg_channel_delete(ctx: lightbulb.Context) -> None:
    cfg = await BurgConfig.get(ctx)
    channel: hikari.GuildTextChannel = ctx.options.channel

    if not cfg.is_channel_burg(channel):
        await ctx.respond(
            error_embed(
                f"Cannot delete channel {channel.name}, it's not a burg channel."
            ),
            flags=hikari.MessageFlag.EPHEMERAL
        )
        return

    await cfg.delete_burg_channel(ctx, channel)
    await ctx.respond(confirm_embed(
        f"Burg channel \"{channel.name}\" deleted by {ctx.author.mention}."
    ))


def main() -> None:
    burgbot.run()


if __name__ == "__main__":
    main()
