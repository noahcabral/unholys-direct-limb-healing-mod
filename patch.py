"""Build a local Direct Limb Healing overlay from an installed Body Health System."""

import argparse
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile


TITLE = "Unholy's Direct Limb Healing Mod"
SCRIPT = "gamedata/scripts/zzz_player_injuries.script"
COOP_SCRIPT = "gamedata/scripts/xrr_patch_bhs.script"
STRINGS = tuple(f"gamedata/configs/text/eng/st_items_{group}.xml" for group in ("medical", "trash"))
REQUIRED = {"medkit", "medkit_army", "medkit_scientic"}
FIRST_AID = REQUIRED | {"stimpack", "stimpack_army", "stimpack_scientic", "rebirth", "survival_kit"}
MEDKIT_ROW = re.compile(r"medkits\.(\w+)\s*=\s*\{([^{}]*)\}\s*(?:--.*)?$")
PARTS = ("head", "torso", "leftarm", "rightarm", "leftleg", "rightleg")
DESCRIPTION_IDS = {
    "medkit": "st_medkit_descr",
    "medkit_army": "st_medkit_army_descr",
    "medkit_scientic": "st_medkit_science_descr",
    "stimpack": "st_stim_impro_descr",
    "stimpack_army": "st_stim_military_descr",
    "stimpack_scientic": "st_stim_scientic_descr",
    "rebirth": "st_rebirth_descr",
    "survival_kit": "st_survival_kit_descr",
    "eft_afak": "st_eft_afak_descr",
}
OLD_HEALING_PROPERTY = re.compile(
    r"(?mi)^[ \t]*%c\[[^\]\n]+\][^\n]*?%c\[[^\]\n]+\][ \t]*"
    r"(?:first[ -]aid|post[ -]heals?)\b[^\n]*\\n[ \t]*\n?"
)
OLD_RECOVERY_PROPERTY = re.compile(
    r"(?mi)^[ \t]*%c\[[^\]\n]+\][^\n]*prolonged body recovery time[^\n]*\\n[ \t]*\n?"
)

FIRST_AID_LUA = """local function restore_first_aid(section, parts, temporary)
    local kit = section and medkits[section]
    if type(kit) ~= "table" then return end

    for part, maximum in pairs(maxhp) do
        local amount = kit[part]
        if amount and amount > 0 then
            -- Keep the temporary HP already present in an older save if it fits.
            local current = math.max(0, math.min(parts[part], maximum))
            local old_bonus = math.max(0, math.min(temporary[part], current))
            local permanent = math.min(maximum, current - old_bonus + amount)
            temporary[part] = math.min(old_bonus, maximum - permanent)
            parts[part] = permanent + temporary[part]
        end
    end
end

function apply_first_aid(section)
    if healingitems then
        restore_first_aid(section, health, timedhp)
    end
end

local function MedicineUsed(section, parts, temporary)
    restore_first_aid(section, parts, temporary)
    return medkits.lastregen, medkits.timedhealth
end
"""

RESCUE_LUA = """    -- The next injury update must see a living permanent head and torso.
    for _, part in ipairs({ "head", "torso" }) do
        local maximum = bhs.maxhp[part]
        local current = math.max(0, math.min(maximum, bhs.health[part]))
        local temporary = math.max(0, math.min(bhs.timedhp[part], current))
        local permanent = math.max(1, current - temporary)
        bhs.timedhp[part] = math.min(temporary, maximum - permanent)
        bhs.health[part] = permanent + bhs.timedhp[part]
    end
    bhs.apply_first_aid(section)
"""


def replace_once(source, pattern, replacement, label):
    matches = list(re.finditer(pattern, source, flags=re.MULTILINE | re.DOTALL))
    if len(matches) != 1:
        raise ValueError(f"{label} was expected once, found {len(matches)}. Unsupported donor.")
    match = matches[0]
    if callable(replacement):
        replacement = replacement(match)
    return source[:match.start()] + replacement + source[match.end():]


def patch_injuries(source, coop):
    strengths = {}

    def rewrite_medkits(match):
        kept_rows = []
        for line in match[0].splitlines():
            declaration = MEDKIT_ROW.fullmatch(line.strip())
            if declaration is None:
                if re.match(r"^\s*medkits\.\w+\s*=", line) and not re.match(
                    r"^\s*medkits\.(?:lastregen|timedhealth)\s*=", line
                ):
                    raise ValueError("Unrecognized BHS medkit declaration")
                kept_rows.append(line)
                continue
            section, fields = declaration.groups()
            if section in FIRST_AID:
                values = dict(re.findall(r"(\w+)\s*=\s*(\d+)", fields))
                strengths[section] = {part: values[part] for part in PARTS if part in values}
                if not strengths[section]:
                    raise ValueError(f"No limb amounts found for {section}")
                kept_rows.append(line)

        if not REQUIRED <= strengths.keys():
            raise ValueError("The donor lacks standard BHS medkit entries")
        if coop:
            strengths["eft_afak"] = strengths["medkit"]
            kept_rows.extend(("medkits.eft_afak = medkits.medkit", ""))
        return "\n".join(kept_rows).rstrip() + "\n\n"

    source = replace_once(source, r"^medkits\s*=\s*\{\}[^\n]*\n.*?(?=^healhelp\s*=)", rewrite_medkits, "medkit table")
    source = replace_once(
        source,
        r"^local easyhealing\s*=\s*(?:true|false)[^\n]*\n",
        "",
        "easy healing option",
    )
    source = replace_once(
        source,
        r"^healhelp\s*=\s*\{\}[^\n]*\n"
        r"(?:(?:[ \t]*(?:healhelp\.\w+\s*=\s*\{[^{}\n]*\}|--[^\n]*)[ \t]*)?\n)*"
        r"^if easyhealing then[^\n]*\n"
        r"(?:^[ \t]*healhelp\.\w+\s*=\s*\{[^{}\n]*\}[^\n]*\n)+"
        r"^end[^\n]*\n",
        "healhelp = {}\n",
        "post-heal table",
    )
    source = replace_once(
        source,
        r"^local function MedicineUsed\([^\n]*\).*?^end[^\n]*\n(?=\s*local function actor_on_item_use\()",
        FIRST_AID_LUA,
        "medicine handler",
    )
    source = replace_once(
        source,
        r"if medkits\[item_name\] or healhelp\[item_name\] then",
        "if medkits[item_name] then",
        "inventory healing preview",
    )
    return source, strengths


def patch_coop(source):
    source = replace_once(
        source,
        r"^local function tables_ok\(bhs\)\s+return bhs and bhs\.health and bhs\.maxhp and bhs\.timedhp and bhs\.medkits\s+end",
        'local function tables_ok(bhs)\n    return bhs and bhs.health and bhs.maxhp and bhs.timedhp\n        and type(bhs.apply_first_aid) == "function"\nend',
        "xrRazom's BHS availability check",
    )
    source = replace_once(
        source,
        r"^-- The healing branch[^\n]*\n.*?^local function apply_medkit\(bhs, section\)\s*\n.*?^end\n\n",
        "",
        "xrRazom's temporary medkit handling",
    )
    return replace_once(
        source,
        r"^    -- BHS writes health -1[^\n]*\n.*?^    apply_medkit\(bhs, section\)\n",
        RESCUE_LUA,
        "xrRazom's revive healing",
    )


def patch_tooltips(source, strengths):
    source = OLD_HEALING_PROPERTY.sub("", source)
    for section, values in strengths.items():
        string_id = DESCRIPTION_IDS[section]
        pattern = re.compile(r'(<string\s+id="' + string_id + r'"\s*>\s*<text>)(.*?)(</text>)', re.DOTALL)
        labels = [f"{part} {values[part]}" for part in ("head", "torso") if part in values]
        for region in ("arm", "leg"):
            left, right = values.get("left" + region), values.get("right" + region)
            if left and left == right:
                labels.append(f"{region}s {left} each")
            else:
                labels.extend(f"{side} {region} {amount}" for side, amount in (("left", left), ("right", right)) if amount)
        line = "%c[pda_white]Restores limb health (" + ", ".join(labels) + ")"

        def add_restoration(match):
            description = OLD_RECOVERY_PROPERTY.sub("", match[2]).rstrip()
            suffix = "\n\t\t" if description.endswith("\\n") else "\\n\n\t\t"
            return match[1] + description + suffix + line + "\\n\n\t\t" + match[3]

        source = pattern.sub(add_restoration, source)
    return source


def transform(path, patch):
    # Latin-1 is a lossless byte mapping for these scripts and XML files.
    # The injected text is ASCII, so legacy game encodings and BOMs survive.
    raw = path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    original = raw.decode("latin-1").replace("\r\n", "\n")
    return patch(original).replace("\n", newline).encode("latin-1")


def donor_layers(args):
    if args.donor:
        if not args.donor.is_dir():
            raise ValueError("--donor must point to a folder containing the installed gamedata")
        return [args.donor]

    modlist = args.mo2 / "profiles" / args.profile / "modlist.txt"
    if not modlist.is_file() or not args.game.is_dir():
        raise ValueError("The MO2 profile or Anomaly folder does not exist")
    excluded = {
        "romeo - direct limb healing",
        TITLE.casefold(),
        *(name.casefold() for name in args.skip_mod),
    }
    return [
        args.mo2 / "overwrite",
        *[
            args.mo2 / "mods" / line[1:]
            for line in modlist.read_text(encoding="utf-8-sig").splitlines()
            if line.startswith("+") and line[1:].casefold() not in excluded
        ],
        args.game,
    ]


def build(args):
    layers = donor_layers(args)

    def resolve(relative):
        return next((layer / relative for layer in layers if (layer / relative).is_file()), None)

    base = resolve(SCRIPT)
    if base is None:
        raise ValueError("Body Health System was not found. Stock Anomaly already uses direct healing.")
    coop = resolve(COOP_SCRIPT)
    script_raw = base.read_bytes()
    newline = "\r\n" if b"\r\n" in script_raw else "\n"
    script, strengths = patch_injuries(script_raw.decode("latin-1").replace("\r\n", "\n"), coop is not None)
    files = {SCRIPT: script.replace("\n", newline).encode("latin-1")}
    if coop is not None:
        files[COOP_SCRIPT] = transform(coop, patch_coop)
    for relative in STRINGS:
        path = resolve(relative)
        if path is not None:
            files[relative] = transform(path, lambda source: patch_tooltips(source, strengths))
    if args.output.suffix.lower() != ".zip":
        raise ValueError("Choose a .zip output path for the MO2 overlay")
    with ZipFile(args.output, "x", compression=ZIP_DEFLATED) as package:
        for relative, contents in files.items():
            package.writestr(relative, contents)
    print(f"Built {args.output} with {len(files)} files.")
    print("Sources:")
    for relative in files:
        print(f"  {resolve(relative)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selectors = parser.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--donor", type=Path, help="Folder above the installed gamedata")
    selectors.add_argument("--mo2", type=Path, help="Portable MO2 base folder containing mods and profiles")
    parser.add_argument("--game", type=Path, help="Anomaly game folder for an MO2 profile")
    parser.add_argument("--profile", help="Exact MO2 profile folder name")
    parser.add_argument("--skip-mod", action="append", default=[], help="Ignore an existing direct-healing overlay by MO2 name")
    parser.add_argument("--output", type=Path, required=True, help="New ZIP path for the generated overlay")
    args = parser.parse_args()
    if args.mo2 and not (args.game and args.profile):
        parser.error("--mo2 needs --game and --profile")
    if args.donor and (args.game or args.profile or args.skip_mod):
        parser.error("--game, --profile, and --skip-mod require --mo2")
    try:
        build(args)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
