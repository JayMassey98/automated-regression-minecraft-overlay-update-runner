## Assert the block at the test position matches. Call with {block:"minecraft:oak_stairs[facing=north]"}.
## Only the listed block states are compared. Run positioned at the test position.

$execute unless block ~ ~ ~ $(block) run function armour:harness/fail {message:"expected $(block) at the test position"}