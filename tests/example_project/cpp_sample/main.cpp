// main.cpp — C++ sample entry point
#include <iostream>
#include "renderer.hpp"

using namespace graphics;

static Scene build_scene() {
    Scene s;
    s.objects.push_back({10, 10, 50, 30, Pixel{255, 0, 0}});
    s.objects.push_back({70, 10, 40, 40, Pixel{0, 255, 0}});
    return s;
}

static void run_demo(Renderer& renderer) {
    Scene scene = build_scene();
    renderer.draw(scene);

    Pixel px = renderer.get_pixel(15, 15);
    std::cout << "Pixel at (15,15): r=" << (int)px.r << "\n";

    renderer.resize(200, 150);
    renderer.draw(scene);
}

int main() {
    Renderer renderer(100, 100);
    run_demo(renderer);
    return 0;
}
