// renderer.cpp — C++ sample: Renderer class implementation
#include "renderer.hpp"
#include <stdexcept>
#include <algorithm>

namespace graphics {

Renderer::Renderer(int width, int height)
    : width_(width), height_(height), cleared_(false)
{
    clear();
}

Renderer::~Renderer() {
    buffer_.clear();
}

void Renderer::clear() {
    buffer_.assign(width_ * height_, Pixel{0, 0, 0});
    cleared_ = true;
}

void Renderer::resize(int new_width, int new_height) {
    if (new_width <= 0 || new_height <= 0) {
        throw std::invalid_argument("Width and height must be positive");
    }
    width_ = new_width;
    height_ = new_height;
    clear();
}

void Renderer::set_pixel(int x, int y, Pixel color) {
    if (!validate_coords(x, y)) return;
    buffer_[y * width_ + x] = color;
    cleared_ = false;
}

Pixel Renderer::get_pixel(int x, int y) const {
    if (!validate_coords(x, y)) return Pixel{0, 0, 0};
    return buffer_[y * width_ + x];
}

bool Renderer::validate_coords(int x, int y) const {
    return x >= 0 && x < width_ && y >= 0 && y < height_;
}

void Renderer::draw_rect(int x, int y, int w, int h, Pixel color) {
    for (int row = y; row < y + h; ++row) {
        for (int col = x; col < x + w; ++col) {
            set_pixel(col, row, color);
        }
    }
}

void Renderer::draw(const Scene& scene) {
    clear();
    for (const auto& obj : scene.objects) {
        draw_rect(obj.x, obj.y, obj.w, obj.h, obj.color);
    }
}

} // namespace graphics
