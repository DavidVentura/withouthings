package dev.davidv.withoutings.ble

import dev.davidv.withoutings.R

/**
 * The glyph the watch shows for each activity in its quick-launch menu.
 *
 * Writing that menu means sending an icon with every entry — the watch keeps
 * the bitmaps and holds no set of its own — so these are the vectors the
 * official app draws, lifted from its resources so a menu written here looks
 * like a menu written there rather than like a second guess.
 *
 * Generated from that app's `activityCategory` table, which names a glyph per
 * category, matched against its `icon_medium_activity_*` drawables. Two
 * activities can share a glyph, so there are fewer files than entries.
 */
val ACTIVITY_GLYPHS: Map<UInt, Int> = mapOf(
    1u to R.drawable.activity_walk, // Walking
    2u to R.drawable.activity_run2, // Running
    3u to R.drawable.activity_hike, // Hiking
    4u to R.drawable.activity_skate, // Skating
    5u to R.drawable.activity_bmx, // BMX
    6u to R.drawable.activity_bike, // Cycling
    7u to R.drawable.activity_swim, // Swimming
    8u to R.drawable.activity_surfing, // Surfing
    9u to R.drawable.activity_kitesurf, // Kitesurfing
    10u to R.drawable.activity_windsurf, // Windsurfing
    11u to R.drawable.activity_bodyboard, // Bodyboard
    12u to R.drawable.activity_tennis, // Tennis
    13u to R.drawable.activity_pingpong, // Table tennis
    14u to R.drawable.activity_squash, // Squash
    15u to R.drawable.activity_badminton, // Badminton
    16u to R.drawable.activity_weightlifting, // Weights
    17u to R.drawable.activity_fitness, // Calisthenics
    18u to R.drawable.activity_elliptical, // Elliptical
    19u to R.drawable.activity_pilates, // Pilates
    20u to R.drawable.activity_basketball, // Basketball
    21u to R.drawable.activity_soccer, // Soccer
    22u to R.drawable.activity_football, // Football
    23u to R.drawable.activity_rugby, // Rugby
    24u to R.drawable.activity_volley, // Volleyball
    25u to R.drawable.activity_waterpolo, // Water polo
    26u to R.drawable.activity_horseriding, // Horse riding
    27u to R.drawable.activity_golf, // Golf
    28u to R.drawable.activity_yoga, // Yoga
    29u to R.drawable.activity_dance, // Dancing
    30u to R.drawable.activity_boxing, // Boxing
    31u to R.drawable.activity_fencing, // Fencing
    32u to R.drawable.activity_wrestling, // Wrestling
    33u to R.drawable.activity_martialarts, // Martial arts
    34u to R.drawable.activity_ski, // Skiing
    35u to R.drawable.activity_snowboard, // Snowboarding
    36u to R.drawable.activity_custom, // Other
    187u to R.drawable.activity_rowing, // Rowing
    188u to R.drawable.activity_zumba, // Zumba
    191u to R.drawable.activity_baseball, // Baseball
    192u to R.drawable.activity_handball, // Handball
    193u to R.drawable.activity_hockey, // Hockey
    194u to R.drawable.activity_icehockey, // Ice hockey
    195u to R.drawable.activity_climbing, // Climbing
    196u to R.drawable.activity_iceskate, // Ice skating
    306u to R.drawable.activity_walk, // Indoor walk
    307u to R.drawable.activity_run2, // Indoor running
)
