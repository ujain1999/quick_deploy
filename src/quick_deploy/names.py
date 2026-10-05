"""Random three-word names like `brave-golden-otter`."""

from __future__ import annotations

import random

ADJECTIVES = """
amber ancient autumn bold brave bright brisk busy calm candid cheerful clever cool cosmic
cozy crimson crisp curious daring dusty eager early easy electric emerald fancy fluffy
fresh friendly frosty fuzzy gentle giant glad golden grand green happy hidden honest
humble icy jolly kind lively lucky lunar mellow merry mighty misty modern noble plain
polite proud purple quick quiet rapid rocky rosy royal rustic salty sandy sharp shiny
silent silver simple sleepy smooth snowy soft solar spicy steady stormy sunny sweet swift
tidy tiny upbeat velvet vivid warm wild windy wise witty young zesty blue red orange
yellow
""".split()

NOUNS = """
acorn anchor apple badger bamboo banana basket beacon bear bell berry biscuit bird breeze
bridge brook button cabin cactus camel candle canoe canyon castle cedar cherry cloud
comet compass cookie coral cotton crane creek cricket daisy delta desert dolphin dragon
dune eagle ember engine falcon feather fern field flame forest fox garden glacier goose
grape hammer harbor hawk hill honey island jacket jungle kettle kite koala ladder lake
lantern leaf lemon lion llama lotus mango maple marble meadow melon meteor mint mitten
moon mountain muffin nest noodle ocean olive orbit orchid otter owl paddle panda parrot
peach pebble pepper pickle pillow pine planet pocket pond puffin pumpkin quill rabbit
radio rain raven reef river robin rocket rose saddle sail salmon scarf shadow shell sky
snow sparrow spoon spring squirrel star stone storm sun teapot thunder ticket tiger toast
tortoise trail tree trumpet tulip turnip valley violin wagon walnut wave whale willow
wind wolf yarn zebra
""".split()


def random_name(taken: set[str] | frozenset[str] = frozenset()) -> str:
    rng = random.SystemRandom()
    while True:
        a, b = rng.sample(ADJECTIVES, 2)
        name = f"{a}-{b}-{rng.choice(NOUNS)}"
        if name not in taken:
            return name
