"""Door codes a family can actually type: a few everyday words.

Media Lab has exactly two codes:

* the **family code** -- one shared code for everyone in the house. It opens
  one permission set: make things, edit them, use and tidy the Library. Every
  device that enters it once stays signed in for a year;
* the **admin code** -- the owner's own, longer code. On top of everything the
  family can do it changes server settings: provider keys, engine installs,
  GPU profiles, and rotating the family code.

Both are random words from ``WORDS`` joined with dashes, e.g.
``maple-otter-lantern-comet``. Four words from a list this size is about 40
bits: with the gate's lockout (door_lockout.py: 3 wrong codes in 10 minutes
shut the prompt for that network for 1 h, then 2 h, 4 h ...) that is out of
reach of online guessing, and it is still something a grandparent can read off
a fridge note. The admin code uses six words (about 60 bits).

The owner may also choose a family code (``media-lab code --set-family``),
even a short one. The server then warns in its log at every start that the
code is short and guessable; the lockout is what keeps a short code safe.

Typing is forgiving. Case, spaces, dashes, dots, commas and underscores are
ignored when the code is checked, so "Maple Otter lantern-COMET" opens the same
door as "maple-otter-lantern-comet". Codes minted before this module existed
(8 letters, or a 4-digit admin code) contain no separators, so they compare
exactly as before and every existing device keeps working until the owner
rotates.

Standard library only: install.sh and the CLI run it without a virtualenv.
"""
from __future__ import annotations

import math
import re
import secrets
import sys

FAMILY_WORDS = 4
ADMIN_WORDS = 6
# A code shorter than this (after normalising) is a legacy or hand-typed code
# the server warns about at start: 4 digits is 10,000 guesses.
MIN_LENGTH = 12

# Common, concrete, easy-to-spell words: 3 to 8 lowercase letters, no names,
# nothing rude, no brand names. Order does not matter; duplicates are dropped.
_WORD_TEXT = """
acorn acrobat actor agent air airport alarm album alcove alley almond alpaca
alpine amber amigo anchor angel angle ankle answer ant antler anvil apex apple
apricot apron aqua arcade arch archer arctic arena armor aroma arrow artist
ash aspen aster atlas atom attic august aunt autumn avenue avid avocado award
axis azure baby bacon badge badger bagel bagpipe baker balcony ball ballad
ballet balloon bamboo banana band banjo banner banquet barber barley barn
barrel basil basin basket bat batch bay beach beacon beagle beam bean bear
beaver bed bee beetle bell belt bench beret berry bicycle bingo birch bird
biscuit bison bistro blanket blaze blender blimp block bloom blossom blue
bluff blush boat bobcat bobsled bold bolt bonnet bonus book boot border
boulder bounce bouquet bowl box bramble brass brave bread breeze brick bridge
bright brine brisk bronze brook broom brush bubble bucket buckle buddy buffalo
bugle bundle bunny burrow bus bush butter button buzz cabana cabin cable
cactus cafe cake calm camel cameo camera camp canal canary candid candle candy
cane canoe canopy canvas canyon cape captain carafe caramel card cargo
carnival carpet carrot cart cartoon cashew castle cat cattle cave cedar celery
cellar cello cement center chalet chalk champ channel chapter charm chart
cheer cheese cheetah chef cherry cherub chess chest chick chime chimney chip
chorus chowder cider cinder cinema circle circus citrus city clam clarinet
clay clever cliff climb clock cloud clover clown coach coast coat cobalt
cobble cocoa coconut cod coffee coin colt comb comet comfort comic compass
concert condor cone cookie cool copper coral cord corn corner cornet cosmic
cotton cougar country cousin cow coyote cozy crab cradle crane crater crayon
cream creek crescent crest cricket crisp croquet crow crown crumb crystal cub
cube cupcake curly curry curtain cushion cycle cymbal dahlia dairy daisy dance
dancer dapper daring dawn dazzle debut deck decoy deer delta denim desert desk
dew dial diamond diary dice dimple diner dinghy dinner dipper disco dish dock
doctor dog dolly dolphin domino donkey donut doodle door dove dragon drawer
dream dress drift drizzle drum duck duet dune dusk dust dynamo eager eagle
early earth easel east easy echo eclipse egg eggplant elbow elder elk elm
ember emerald emu endive energy engine envoy epic equal escape essay estate
ether evening event exit expert fable fabric facet fairy falafel falcon fan
fancy farm farmer fast feast feather feline fence fennel fern ferret ferry
festival festive fiber fiddle field fiesta fig figure film finale finch finger
fire fish fjord flag flame flannel flash fleece fleet flint flipper flock
flora flower fluffy flute focus fog folder folk forest fork fort fossil fox
frame fresh friday friend frog frolic frost frosty fruit fudge fuzzy gadget
galaxy gallery galley gallon game garage garden garland garlic garnet gate
gazebo gazelle gecko gem gentle geyser giant gift ginger giraffe glacier glad
glass glider glimmer glitter globe gloss glove glow gnome goat goblet golden
golf gondola goose gopher gorilla gourd grain grand granite granola grape
graph grass gravel gravy green griddle grill grotto grove guava guide guitar
gumdrop gust habit halibut halo hamlet hammer hammock hamster handle happy
harbor hare harmony harp harvest hat hatch haven hawk hazel hazelnut hedge
helium helmet hen hero heron hickory hiker hill hippo hobby hockey holiday
hollow honest honey hoodie hook hoop horizon horn horse hotel hound hour hub
humble hummus husky ice iceberg icicle icon idea igloo image inch index indigo
ink inkwell insect iris iron island item ivory ivy jacket jade jaguar jam jar
jasmine jay jazz jeans jelly jersey jet jewel jigsaw jingle jockey jolly
journal journey joy jubilee judge juice july jumbo june jungle junior juniper
kale kayak keen kelp kernel kettle key keyboard kid kilo kimono kind kingdom
kitchen kite kitten kiwi knight knot koala label ladder lagoon lake lamb lamp
lantern lapel larch large lark laser lasso latch laurel lava lavender lawn
layer leaf leeway legend lemon lemur lens lentil leopard letter lettuce level
lever library lilac lily lilypad lime limerick linen lion liquid little lively
lizard llama lobby lobster locket lodge logic loop lotus lucky lullaby lumber
lunar lunch lynx lyric macaw machine mackerel magic magnet magpie mammoth
mango manor mantle map maple marble march margin marina market marmot marsh
mascot mask meadow medal mellow melody melon memo mentor menu merry mesa metal
meteor midnight mighty mile mill mimosa mineral minnow mint minute mirror
mission mist mitten mixer mocha model modest molasses mole moment monarch
monday monkey moon moose morning mosaic moss moth motor mountain mouse muffin
mule mural museum music muslin mustard mystic nacho napkin narwhal navy neat
nebula nectar needle neon nest net newt nickel night nimble noble nomad noodle
noon north notebook nougat novel nugget number nutmeg oak oar oasis oat
oatmeal ocean ocelot october office olive omega omelet onion opal opera orange
orbit orbiter orchard orchid oregano origin otter outlet oval owl oxygen
oyster pacific paddle pagoda paint painter paisley palace palm pan panda panel
panorama panther papaya paper papyrus parade parcel park parrot parsley pasta
pastry patch path patio peach peacock peanut pear pearl pebble pecan pedal
pelican pen pencil penguin pepper petal pewter photo piano pickle picnic pie
pier pig pigeon pillow pilot pinball pine pinwheel pioneer piper pixel pizza
plaid planet plate plaza plum plume pocket poem polar polite pollen poncho
pond pony poodle popcorn poppy porch porridge portal poster potato pottery
powder prairie pretzel prince prism prize proud pudding puddle pueblo puffin
pulse pumpkin pupil puppet puppy purple puzzle quail quartz queen quest quiche
quick quiet quilt quiver rabbit raccoon radar radiant radio radish raft rain
rainbow raisin rake ranch ranger rapid raven ravioli recipe record red reef
regal relay relish rhubarb ribbon rice riddle ridge ring ripple river riviera
road robin robot rock rocket rodeo roof rooster rope rose rosemary rosy rotor
route royal ruby rug ruler rumble runway rustic saddle safari saffron saga
sail sailor salad salmon salsa salt sample sand sandal sapphire sardine satin
sauce saucer savanna scale scallop scarf scarlet school science scooter scout
sea seal season seed sequin sesame shadow shark sheep shelf shell shelter
sherbet shiny ship shore shovel shrimp sierra signal silk silver simple singer
sketch ski sky sled sleepy slipper sloth smile smooth snack snail snorkel snow
snowy soccer sock sofa soft solar song sonnet sorbet soup south spice spider
spinach spindle spiral splash sponge spool spoon spring sprout spruce square
squash squid stable stadium stamp star stardust starfish station statue steady
steam stencil stone stool storm story stove stream street string studio sturdy
sugar suitcase summer summit sun sundae sunday sunny sunrise sunset super surf
swallow swan sweater sweet swift sycamore syrup table taco tadpole tailor
talent tall tamarind tango tapestry target tartan tea teacher teacup teal
teapot tempo tender tennis tent thimble thistle thunder thyme ticket tide tidy
tiger tile timber tiny toast toboggan toffee token tomato topaz topsoil torch
tortoise toucan towel tower town toy tractor trail train trapeze treasure tree
trellis trolley trophy tropic trout truck truffle trumpet tuba tulip tuna
tundra tunnel turkey turnip turtle tutor tuxedo tweed twig ukulele umbrella
unicorn unit universe upbeat valley vanilla vapor vase velour velvet venus
verbena verse vessel video village vintage vinyl violet violin visit vista
vivid voice volcano voyage wafer waffle wagon walkway walnut walrus waltz
wander warbler warm wasp water wave wavy weasel weaver week whale wheat wheel
whisker whistle white wicker widget wild willow wind window windy winner
winter wise witty wizard wolf wombat wonder wood wool yacht yak yard yarn year
yellow yodel yogurt yonder young zebra zenith zephyr zeppelin zesty zigzag
zinnia zipper zone zucchini
"""

WORDS: tuple[str, ...] = tuple(dict.fromkeys(
    w for w in _WORD_TEXT.split() if re.fullmatch(r"[a-z]{3,8}", w)))

_SEPARATORS = re.compile(r"[\s\-_.,·]+")


def bits_per_word() -> float:
    return math.log2(len(WORDS))


def mint(words: int, rng=None) -> str:
    """``words`` random words from WORDS, dash-joined, from a CSPRNG."""
    if words < 1:
        raise ValueError("a code needs at least one word")
    rng = rng or secrets.SystemRandom()
    return "-".join(rng.choice(WORDS) for _ in range(words))


def mint_family(rng=None) -> str:
    return mint(FAMILY_WORDS, rng)


def mint_admin(rng=None) -> str:
    return mint(ADMIN_WORDS, rng)


def normalize(raw: str | None) -> str:
    """The comparable form of a typed or stored code: separators dropped, upper case."""
    return _SEPARATORS.sub("", raw or "").upper()


def is_weak(code: str | None) -> bool:
    """True for a code short enough to be guessed online (legacy 4-8 chars)."""
    return len(normalize(code)) < MIN_LENGTH


def main(argv: list[str] | None = None) -> int:
    """``python -m media_lab_core.family_code [family|admin]`` prints one new code."""
    argv = sys.argv[1:] if argv is None else argv
    kind = argv[0] if argv else "family"
    if kind not in ("family", "admin"):
        print("usage: python -m media_lab_core.family_code [family|admin]", file=sys.stderr)
        return 2
    print(mint_admin() if kind == "admin" else mint_family())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
