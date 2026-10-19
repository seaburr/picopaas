"""Generates `<adjective>-<animal>-<7 char suffix>` hostnames, e.g. clever-robin-a1b2c3d.

Matches ARCHITECTURE.md: "<adjective>-<animal>-<7 chars of [a-z0-9]>.raisehellpraisedale.xyz", with a
uniqueness check in the DB. This module only builds the label; the caller (apps router)
appends the domain and checks the `apps.hostname` column for collisions.
"""
import secrets
import string

from sqlalchemy.orm import Session

from app import models

ADJECTIVES = [
    "clever", "quiet", "bold", "lucky", "swift", "gentle", "bright", "cosmic", "lazy", "brave",
    "calm", "eager", "fuzzy", "jolly", "mighty", "nimble", "proud", "silly", "spry", "witty",
    "zesty", "amber", "coral", "golden", "little", "shiny", "sleepy", "sneaky", "sunny", "tiny",
    "agile", "airy", "alpine", "ancient", "arctic", "ashen", "autumn", "azure", "balmy", "bashful",
    "breezy", "bubbly", "burly", "candid", "cheery", "chilly", "chipper", "civil", "clear", "cozy",
    "crisp", "curious", "dainty", "dapper", "dashing", "dewy", "dizzy", "dusty", "earnest",
    "elegant", "fancy", "festive", "fierce", "fluffy", "foggy", "frosty", "gallant", "giddy",
    "glad", "gleaming", "glossy", "graceful", "grand", "groovy", "happy", "hardy", "hasty", "hazy",
    "hearty", "hilly", "humble", "icy", "ivory", "jaunty", "jazzy", "keen", "kind", "knotty",
    "lanky", "leafy", "lively", "lofty", "lunar", "mellow", "merry", "minty", "misty", "modest",
    "mossy", "murky", "narrow", "neat", "noble", "nutty", "oaken", "odd", "olive", "peppy", "perky",
    "plucky", "plump", "polite", "pretty", "prickly", "primal", "puffy", "punchy", "quaint",
    "quick", "rapid", "rosy", "rustic", "sandy", "savvy", "scenic", "serene", "shaggy", "sharp",
    "shy", "silent", "silver", "sleek", "smoky", "snappy", "snowy", "solar", "speedy", "spicy",
    "spotty", "steady", "stormy", "sturdy", "sugary", "super", "tame", "tangy", "teal", "tender",
    "thrifty", "tidy", "toasty", "tough", "trusty", "twinkly", "upbeat", "velvet", "vivid", "wavy",
    "wild", "windy", "wise", "wooly", "young", "zany", "zippy",
]

ANIMALS = [
    "bluejay", "cardinal", "cheetah", "dormouse", "flamingo", "gnu", "heifer", "lamprey", "mantis", "oriole",
    "robin", "otter", "panda", "falcon", "koala", "badger", "heron", "lynx", "mole", "newt", "orca",
    "puffin", "quail", "raven", "seal", "shrew", "tapir", "vole", "walrus", "yak", "zebra", "crane",
    "finch", "gecko", "ibis", "jackal", "kiwi", "lemur", "moth", "wren", "alpaca", "antelope",
    "ant", "bat", "beaver", "bee", "bison", "bobcat", "bunny", "camel", "capybara", "cat",
    "chipmunk", "cobra", "condor", "cougar", "coyote", "crab", "cricket", "crow", "deer", "dingo",
    "dolphin", "donkey", "dove", "dragonfly", "duck", "eagle", "eel", "elk", "emu", "ermine",
    "ferret", "firefly", "fox", "frog", "gazelle", "gibbon", "giraffe", "goat", "goose", "gopher",
    "gorilla", "grouse", "gull", "hamster", "hare", "hawk", "hedgehog", "hippo", "horse", "hornet",
    "hyena", "iguana", "impala", "jaguar", "jay", "kestrel", "kitten", "koi", "lark", "lamb",
    "leopard", "lion", "lizard", "llama", "lobster", "loon", "macaw", "magpie", "manatee", "marmot",
    "meerkat", "mink", "mouse", "mule", "narwhal", "ocelot", "osprey", "ostrich", "owl", "oyster",
    "parrot", "peacock", "pelican", "penguin", "pheasant", "pigeon", "pony", "possum", "python",
    "rabbit", "raccoon", "reindeer", "rhino", "salmon", "sparrow", "squid", "squirrel", "stork",
    "swan", "swallow", "tiger", "toad", "trout", "turkey", "turtle", "viper", "vulture", "wallaby",
    "wasp", "weasel", "whale", "wolf", "wombat", "woodpecker",
]

# Labels that must never be handed to an app (platform/infra hostnames). Generated names always have the
# adjective-animal-suffix shape, so this is a belt-and-braces guard.
RESERVED_HOSTNAMES = frozenset({"platform", "picopaas", "lb", "www", "api"})

_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits
_SUFFIX_LEN = 7
_MAX_ATTEMPTS = 50


def _random_hostname() -> str:
    adjective = secrets.choice(ADJECTIVES)
    animal = secrets.choice(ANIMALS)
    suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(_SUFFIX_LEN))
    return f"{adjective}-{animal}-{suffix}"


def generate_unique_hostname(db: Session) -> str:
    """Generate a hostname label, retrying on collision against apps.hostname."""
    for _ in range(_MAX_ATTEMPTS):
        candidate = _random_hostname()
        exists = db.query(models.App.id).filter(models.App.hostname == candidate).first()
        if candidate not in RESERVED_HOSTNAMES and not exists:
            return candidate
    raise RuntimeError("could not generate a unique hostname after many attempts")
