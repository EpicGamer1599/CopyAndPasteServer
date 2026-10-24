"""Short, plain-language summaries for Discord's command screens."""
from datetime import datetime

import discord

from structure import TYPE_NAMES


def safe(value, limit=200):
    return discord.utils.escape_markdown(str(value))[:limit]


def template_name(snapshot):
    return snapshot.get("label") or snapshot["source"]["name"]


def template_label(snapshot):
    date = datetime.fromisoformat(snapshot["created_at"]).strftime("%d %b %Y %H:%M UTC")
    return f"{template_name(snapshot)[:45]} · {date} · {snapshot['id'][:6]}"[:100]


def totals(snapshot):
    categories = sum(c["type"] == 4 for c in snapshot["channels"])
    channels = len(snapshot["channels"]) - categories
    return f"{categories} {'category' if categories == 1 else 'categories'} and {channels} {'channel' if channels == 1 else 'channels'}"


def layout_text(snapshot, limit=3000):
    lines = ["**Outside categories**"]
    for channel in snapshot["channels"]:
        name = safe(channel["name"], 100)
        if channel["type"] == 4:
            lines.append(f"\n**{name}**")
        else:
            marker = "🔊" if channel["type"] in (2, 13) else "#"
            lines.append(f"  {marker} {name} · {TYPE_NAMES.get(channel['type'], 'unsupported')}")
    result = "\n".join(lines)
    if len(result) > limit:
        # Truncate at a complete line so Markdown stays well formed.
        result = result[:limit].rsplit("\n", 1)[0] + "\n… Full layout is in the attached file."
    return result


def template_embed(snapshot):
    embed = discord.Embed(title=f"Saved layout: {template_name(snapshot)}"[:256],
                          description=layout_text(snapshot), colour=discord.Colour.blurple())
    embed.add_field(name="Includes", value=totals(snapshot), inline=True)
    embed.add_field(name="Copied from", value=safe(snapshot["source"]["name"]), inline=True)
    embed.set_footer(text="A template is a saved channel layout. Messages and members are never copied.")
    return embed


def blocker_text(reason):
    if "mapping" in reason or "colliding destination role" in reason:
        return "Choose a matching role using Match permissions."
    if "Member-specific" in reason:
        return "This channel uses permissions for individual people. Change these to role permissions in the original server, then scan again."
    if "Parent category is blocked" in reason:
        return "Its category cannot be copied yet. Fix the category first."
    if "Community" in reason:
        return "Enable Community in the receiving server's settings for this channel type."
    if "bitrate" in reason:
        return "The receiving server's voice quality limit is lower than this channel's setting."
    if "parent category was inaccessible" in reason:
        return "The bot could not read its category. Give it View Channels there and scan again."
    return reason


def error_text(message):
    if "uncertain create request" in message:
        return ("A previous channel request may have reached Discord, but I did not receive confirmation. "
                "Copying is paused to avoid duplicates. Ask the person running CAPS to check the saved progress before retrying.")
    if "journal" in message or "Malformed snapshot" in message:
        return "The saved layout or its copy progress needs checking. Ask the person running CAPS to inspect it or restore a backup. No new copy was started."
    if "Snapshot changed since" in message:
        return "This saved layout changed after its copy started. Ask the person running CAPS to restore the original before continuing."
    if "Previously created channel" in message or "Previously copied" in message:
        return "A channel from the earlier copy was removed or changed. Ask the person running CAPS to check the receiving server before continuing."
    return message


def report_summary(report):
    problems = any(word in report for word in ("FAILED", "MISMATCH", "outcome uncertain", "interrupted"))
    if problems:
        return ("Copy needs attention", "Some steps could not finish. Your progress is saved. "
                "Read the attached report before retrying `/copy`; ask the bot owner for help if it mentions an uncertain result.")
    if "BLOCKED" in report:
        return ("Copy finished with skipped channels", "The channels that could be copied are ready. "
                "Some were skipped; the attached report explains why. Use `/copy` again to fix permission matches and continue.")
    return "Copy finished", "Your saved layout is ready in this server. The attached report lists any settings Discord could not copy."
