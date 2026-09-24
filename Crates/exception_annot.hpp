#ifndef INDUSTRIAL_HEATING_CONTROL_HPP_INCLUDED
#define INDUSTRIAL_HEATING_CONTROL_HPP_INCLUDED

#include <array>
#include <cstddef>
#include <fstream>
#include <mutex>
#include <string>
#include <utility>

namespace industrial_heating
{

/*
 * Correction Matrix Delta
 *
 * Computes:
 *
 *     Delta[i,j] = sum(k=0..3) modifier_A[i,k] * modifier_B[k,j]
 *
 * and applies the resulting correction directly to baseline.
 *
 * Requirements:
 *   - baseline contains N*N floats
 *   - modifier_A is N*4
 *   - modifier_B is 4*N
 *   - no heap allocation
 *   - no external math libraries
 *   - fixed-size stack temporaries only
 *
 * The caller owns modifier_A and modifier_B. They may therefore be
 * ordinary stack-allocated arrays such as:
 *
 *     float modifier_A[N][4];
 *     float modifier_B[4][N];
 */
template <std::size_t N>
inline void apply_correction_matrix_delta(
    float (&baseline)[N * N],
    const float (&modifier_A)[N][4],
    const float (&modifier_B)[4][N]) noexcept
{
    /*
     * A temporary is used so that the calculation is based on the
     * original baseline values even if future correction semantics
     * require more complicated transformations.
     *
     * The actual correction matrix is N*N floats and therefore lives
     * on the caller's thread stack. No heap allocation occurs.
     */
    float delta[N * N] = {};

    for (std::size_t row = 0; row < N; ++row)
    {
        for (std::size_t col = 0; col < N; ++col)
        {
            /*
             * Factorized transformation:
             *
             * A[row][0] * B[0][col]
             * + A[row][1] * B[1][col]
             * + A[row][2] * B[2][col]
             * + A[row][3] * B[3][col]
             */
            delta[row * N + col] =
                modifier_A[row][0] * modifier_B[0][col] +
                modifier_A[row][1] * modifier_B[1][col] +
                modifier_A[row][2] * modifier_B[2][col] +
                modifier_A[row][3] * modifier_B[3][col];
        }
    }

    /*
     * Apply the computed delta in-place.
     */
    for (std::size_t i = 0; i < N * N; ++i)
    {
        baseline[i] += delta[i];
    }
}


/*
 * Variant operating on a flat baseline pointer.
 *
 * This overload is useful when the baseline storage is supplied by an
 * embedded system rather than represented as a C++ array.
 *
 * Preconditions:
 *   baseline != nullptr
 *   modifier_A != nullptr
 *   modifier_B != nullptr
 *
 * No dynamic allocation is performed.
 */
template <std::size_t N>
inline void apply_correction_matrix_delta(
    float* baseline,
    const float (*modifier_A)[4],
    const float (*modifier_B)[N]) noexcept
{
    if (baseline == nullptr ||
        modifier_A == nullptr ||
        modifier_B == nullptr)
    {
        return;
    }

    float delta[N * N] = {};

    for (std::size_t row = 0; row < N; ++row)
    {
        for (std::size_t col = 0; col < N; ++col)
        {
            delta[row * N + col] =
                modifier_A[row][0] * modifier_B[0][col] +
                modifier_A[row][1] * modifier_B[1][col] +
                modifier_A[row][2] * modifier_B[2][col] +
                modifier_A[row][3] * modifier_B[3][col];
        }
    }

    for (std::size_t i = 0; i < N * N; ++i)
    {
        baseline[i] += delta[i];
    }
}


/*
 * Thread-safe baseline manager.
 *
 * The baseline itself is stored inline in the manager. Consequently,
 * constructing:
 *
 *     BaselineManager<32> manager(...);
 *
 * creates a 32*32 float baseline without any heap allocation.
 *
 * The caller should therefore normally instantiate this object with
 * static storage duration or in an appropriate memory region when N
 * is large enough that stack usage would be undesirable.
 */
template <std::size_t N>
class BaselineManager
{
public:
    using baseline_array = std::array<float, N * N>;

    /*
     * The path is copied into a fixed-size character buffer.
     *
     * This avoids std::string allocation entirely. Increase
     * ConfigurationPathCapacity if the target system requires longer
     * paths.
     */
    static constexpr std::size_t ConfigurationPathCapacity = 256U;

    BaselineManager(
        const char* configuration_path,
        const baseline_array& initial_baseline = baseline_array{}) noexcept
        : baseline_(initial_baseline),
          configuration_path_{},
          configuration_path_length_(0U)
    {
        if (configuration_path == nullptr)
        {
            return;
        }

        while (configuration_path[configuration_path_length_] != '\0' &&
               configuration_path_length_ + 1U <
                   ConfigurationPathCapacity)
        {
            ++configuration_path_length_;
        }

        for (std::size_t i = 0; i < configuration_path_length_; ++i)
        {
            configuration_path_[i] = configuration_path[i];
        }

        configuration_path_[configuration_path_length_] = '\0';
    }

    BaselineManager(const BaselineManager&) = delete;
    BaselineManager& operator=(const BaselineManager&) = delete;

    BaselineManager(BaselineManager&&) = delete;
    BaselineManager& operator=(BaselineManager&&) = delete;

    /*
     * Thread-safe correction update.
     *
     * modifier_A and modifier_B are expected to be caller-owned,
     * stack-allocated configuration buffers.
     *
     * No heap allocation occurs during the computation.
     */
    void update(
        const float (&modifier_A)[N][4],
        const float (&modifier_B)[4][N]) noexcept
    {
        std::lock_guard<std::mutex> lock(mutex_);

        apply_correction_matrix_delta<N>(
            baseline_.data(),
            modifier_A,
            modifier_B);
    }

    /*
     * Thread-safe update using flat pointers.
     *
     * Useful for embedded code where configuration buffers are
     * supplied from existing memory.
     */
    void update(
        const float (*modifier_A)[4],
        const float (*modifier_B)[N]) noexcept
    {
        std::lock_guard<std::mutex> lock(mutex_);

        apply_correction_matrix_delta<N>(
            baseline_.data(),
            modifier_A,
            modifier_B);
    }

    /*
     * Thread-safe direct baseline element access.
     *
     * Returns false for an invalid coordinate.
     */
    bool set(
        std::size_t row,
        std::size_t column,
        float value) noexcept
    {
        if (row >= N || column >= N)
        {
            return false;
        }

        std::lock_guard<std::mutex> lock(mutex_);

        baseline_[row * N + column] = value;
        return true;
    }

    /*
     * Thread-safe single-value read.
     *
     * Returns false for an invalid coordinate.
     */
    bool get(
        std::size_t row,
        std::size_t column,
        float& value) const noexcept
    {
        if (row >= N || column >= N)
        {
            return false;
        }

        std::lock_guard<std::mutex> lock(mutex_);

        value = baseline_[row * N + column];
        return true;
    }

    /*
     * Thread-safe copy of the entire baseline.
     *
     * The destination is supplied by the caller, so this operation
     * does not allocate memory.
     */
    void copy_baseline(baseline_array& destination) const noexcept
    {
        std::lock_guard<std::mutex> lock(mutex_);

        destination = baseline_;
    }

    /*
     * Thread-safe persistence.
     *
     * File format:
     *
     *   N*N binary float values, in row-major order.
     *
     * A binary format is used because this is intended for embedded
     * calibration data and avoids text conversion overhead and
     * ambiguity.
     *
     * The file is written from a local snapshot so the mutex does not
     * remain locked while the potentially slow filesystem operation
     * is underway.
     *
     * The snapshot itself is fixed-size and requires no heap
     * allocation.
     */
    bool save() const noexcept
    {
        baseline_array snapshot;

        {
            std::lock_guard<std::mutex> lock(mutex_);

            if (configuration_path_length_ == 0U)
            {
                return false;
            }

            snapshot = baseline_;
        }

        std::ofstream output(
            configuration_path_,
            std::ios::binary | std::ios::trunc);

        if (!output.is_open())
        {
            return false;
        }

        output.write(
            reinterpret_cast<const char*>(snapshot.data()),
            static_cast<std::streamsize>(
                sizeof(float) * snapshot.size()));

        if (!output.good())
        {
            return false;
        }

        output.flush();

        return output.good();
    }

    /*
     * Allows the destination path to be changed without dynamic
     * allocation.
     */
    bool set_configuration_path(
        const char* configuration_path) noexcept
    {
        if (configuration_path == nullptr)
        {
            return false;
        }

        char temporary_path[ConfigurationPathCapacity] = {};
        std::size_t length = 0U;

        while (configuration_path[length] != '\0' &&
               length + 1U < ConfigurationPathCapacity)
        {
            temporary_path[length] = configuration_path[length];
            ++length;
        }

        /*
         * Reject paths that do not fit instead of silently truncating
         * them.
         */
        if (configuration_path[length] != '\0')
        {
            return false;
        }

        temporary_path[length] = '\0';

        {
            std::lock_guard<std::mutex> lock(mutex_);

            for (std::size_t i = 0; i <= length; ++i)
            {
                configuration_path_[i] = temporary_path[i];
            }

            configuration_path_length_ = length;
        }

        return true;
    }

    /*
     * Returns the matrix dimension.
     */
    static constexpr std::size_t size() noexcept
    {
        return N;
    }

private:
    mutable std::mutex mutex_;

    /*
     * Inline fixed-size storage.
     *
     * No heap allocation is associated with the baseline.
     */
    baseline_array baseline_;

    /*
     * Fixed-size configuration path storage.
     *
     * This avoids std::string and therefore avoids heap allocation
     * during construction and normal operation.
     */
    char configuration_path_[ConfigurationPathCapacity];

    std::size_t configuration_path_length_;
};


/*
 * Example helper demonstrating the intended stack-based update
 * configuration.
 *
 * This is deliberately a function template rather than part of the
 * manager so that modifier_A and modifier_B remain automatic
 * (stack-allocated) objects.
 *
 * The actual values of the configuration buffers are application
 * specific and should normally come from the heating controller's
 * calibration/configuration subsystem.
 */
template <std::size_t N>
inline void apply_stack_configuration(
    BaselineManager<N>& manager) noexcept
{
    float modifier_A[N][4] = {};
    float modifier_B[4][N] = {};

    /*
     * Populate modifier_A and modifier_B here.
     *
     * Example:
     *
     * modifier_A[0][0] = ...;
     * modifier_B[0][0] = ...;
     *
     * No heap allocation is performed by these arrays.
     */

    manager.update(modifier_A, modifier_B);
}

} // namespace industrial_heating

#endif // INDUSTRIAL_HEATING_CONTROL_HPP_INCLUDED
