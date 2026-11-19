## Wait until the arena chunk is loaded, then run the tests (gives up after about 20 seconds).

scoreboard players add #attempts armour.test 1
execute if score #attempts armour.test matches 200.. run return run say ARMOUR_ERROR the test arena chunk never loaded
execute unless loaded 8 150 8 run return run schedule function armour:harness/wait_for_arena 2t
function armour:harness/run