## Entry point. Run once on a running server to start the whole test suite:
##   /function armour:harness/start
## Results are written to the server log as ARMOUR_FAIL / ARMOUR_SKIP / ARMOUR_COUNTS / ARMOUR_RESULT lines.

scoreboard objectives add armour.test dummy
scoreboard players set #attempts armour.test 0

# The arena is the chunk containing 0 0, high above any terrain (see harness/run).
forceload add 0 0 15 15
function armour:harness/wait_for_arena