cmake_minimum_required(VERSION 3.16)
project(engine-sim-headless CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(ENGINE_SIM_DIR "" CACHE PATH "engine-sim checkout with headless.patch applied")
if (NOT ENGINE_SIM_DIR)
  message(FATAL_ERROR "Pass -DENGINE_SIM_DIR=/path/to/engine-sim")
endif()
set(ES ${ENGINE_SIM_DIR})
# MSVC-isms: __forceinline, implicit <cstring>/<cmath>/<cstdlib>
add_compile_options(-D__forceinline=inline "SHELL:-include cstring" "SHELL:-include cmath" "SHELL:-include cstdlib" "SHELL:-include math.h" "-D__int64=long long" -w)

file(GLOB SCS_SRC ${ES}/dependencies/submodules/simple-2d-constraint-solver/src/*.cpp)
add_library(scs STATIC ${SCS_SRC})

file(GLOB CSV_SRC ${ES}/dependencies/submodules/csv-io/src/*.cpp)
list(FILTER CSV_SRC EXCLUDE REGEX "main\\.cpp$")
add_library(csvio STATIC ${CSV_SRC})

add_library(engine-sim STATIC
    ${ES}/src/audio_buffer.cpp
    ${ES}/src/camshaft.cpp
    ${ES}/src/crankshaft.cpp
    ${ES}/src/combustion_chamber.cpp
    ${ES}/src/connecting_rod.cpp
    ${ES}/src/convolution_filter.cpp
    ${ES}/src/cylinder_bank.cpp
    ${ES}/src/cylinder_head.cpp
    ${ES}/src/delay_filter.cpp
    ${ES}/src/derivative_filter.cpp
    ${ES}/src/direct_throttle_linkage.cpp
    ${ES}/src/dynamometer.cpp
    ${ES}/src/engine.cpp
    ${ES}/src/exhaust_system.cpp
    ${ES}/src/feedback_comb_filter.cpp
    ${ES}/src/filter.cpp
    ${ES}/src/fuel.cpp
    ${ES}/src/function.cpp
    ${ES}/src/gas_system.cpp
    ${ES}/src/gaussian_filter.cpp
    ${ES}/src/governor.cpp
    ${ES}/src/ignition_module.cpp
    ${ES}/src/impulse_response.cpp
    ${ES}/src/intake.cpp
    ${ES}/src/jitter_filter.cpp
    ${ES}/src/leveling_filter.cpp
    ${ES}/src/low_pass_filter.cpp
    ${ES}/src/part.cpp
    ${ES}/src/piston.cpp
    ${ES}/src/piston_engine_simulator.cpp
    ${ES}/src/simulator.cpp
    ${ES}/src/standard_valvetrain.cpp
    ${ES}/src/starter_motor.cpp
    ${ES}/src/synthesizer.cpp
    ${ES}/src/throttle.cpp
    ${ES}/src/transmission.cpp
    ${ES}/src/utilities.cpp
    ${ES}/src/valvetrain.cpp
    ${ES}/src/vehicle.cpp
    ${ES}/src/vehicle_drag_constraint.cpp
    ${ES}/src/vtec_valvetrain.cpp
)
target_include_directories(engine-sim PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/stubs ${ES}/dependencies/submodules ${ES}/include)
target_link_libraries(engine-sim PUBLIC scs csvio)

option(WITH_SCRIPT "build piranha + .mr script compiler" ON)
if (WITH_SCRIPT)
  set(PIRANHA_ENABLED ON)
  add_subdirectory(${ES}/dependencies/submodules/piranha piranha)
  add_library(engine-sim-script STATIC
    ${ES}/scripting/src/channel_types.cpp
    ${ES}/scripting/src/compiler.cpp
    ${ES}/scripting/src/engine_context.cpp
    ${ES}/scripting/src/language_rules.cpp
  )
  target_compile_definitions(engine-sim-script PUBLIC ATG_ENGINE_SIM_PIRANHA_ENABLED)
  target_include_directories(engine-sim-script PUBLIC ${ES}/dependencies/submodules)
  target_link_libraries(engine-sim-script PUBLIC engine-sim piranha)
endif()

add_executable(headless_driver driver.cpp)
target_link_libraries(headless_driver PRIVATE engine-sim $<$<BOOL:${WITH_SCRIPT}>:engine-sim-script> pthread)
