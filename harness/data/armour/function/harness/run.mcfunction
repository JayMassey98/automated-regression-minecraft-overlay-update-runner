## Run every test once, then report. Tests run positioned at the arena test position (8 150 8).

scoreboard players set #passed armour.test 0
scoreboard players set #failed armour.test 0
scoreboard players set #skipped armour.test 0

# Arena floor
fill 0 149 0 15 149 15 minecraft:stone

# #armour:run_tests lists each tests pack's generated runner (scripts/generate_test_runner.py)
function #armour:run_tests

function armour:harness/finish