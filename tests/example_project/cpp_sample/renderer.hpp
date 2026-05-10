// renderer.hpp — C++ sample: Renderer class declaration
#pragma once
#include <vector>

namespace graphics {

struct Pixel {
    unsigned char r, g, b;
};

struct SceneObject {
    int x, y, w, h;
    Pixel color;
};

struct Scene {
    std::vector<SceneObject> objects;
};

class Renderer {
public:
    Renderer(int width, int height);
    ~Renderer();

    void draw(const Scene& scene);
    void clear();
    void resize(int new_width, int new_height);
    void set_pixel(int x, int y, Pixel color);
    Pixel get_pixel(int x, int y) const;

private:
    int width_;
    int height_;
    bool cleared_;
    std::vector<Pixel> buffer_;

    bool validate_coords(int x, int y) const;
    void draw_rect(int x, int y, int w, int h, Pixel color);
};

} // namespace graphics
