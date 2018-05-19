// Copyright 2018 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include "cc/thread_safe_queue.h"

#include <map>
#include <thread>
#include <vector>

#include "absl/base/thread_annotations.h"
#include "absl/synchronization/mutex.h"
#include "absl/time/clock.h"
#include "absl/time/time.h"
#include "gmock/gmock.h"
#include "gtest/gtest.h"

namespace minigo {
namespace {

// Verify that the queue is a FIFO.
TEST(CoordTest, Ordering) {
  ThreadSafeQueue<int> q;

  q.Push(1);
  q.Push(2);
  q.Push(3);
  EXPECT_EQ(1, q.Pop());

  int x;
  EXPECT_TRUE(q.TryPop(&x));
  EXPECT_EQ(2, x);

  EXPECT_EQ(3, q.Pop());

  EXPECT_FALSE(q.TryPop(&x));
}

// Verify that the queue works with move-only objects.
TEST(CoordTest, MoveOnlyObject) {
  // A simple move-only type.
  struct MoveOnly {
    explicit MoveOnly(int x) : x(x) {}
    MoveOnly(const MoveOnly&) = delete;
    MoveOnly& operator=(const MoveOnly&) = delete;
    MoveOnly(MoveOnly&& other) : x(other.x) { other.x = -1; }
    MoveOnly& operator=(MoveOnly&& other) {
      x = other.x;
      other.x = -1;
      return *this;
    }
    int x;
  };
  ThreadSafeQueue<MoveOnly> q;

  q.Push(MoveOnly(42));
  EXPECT_EQ(42, q.Pop().x);
}

// Verify multithreading.
TEST(CoordTest, Multithreading) {
  ThreadSafeQueue<int> q;

  // Push a bunch of ints onto the queue.
  std::map<int, int> pushed;
  for (int i = 0; i < 10000; ++i) {
    pushed[i] = 1;
    q.Push(i);
  }

  absl::Mutex m;
  std::map<int, int> popped GUARDED_BY(&m);

  // Pop the ints off the queue on multiple threads.
  std::vector<std::thread> threads;
  for (int i = 0; i < 10; ++i) {
    threads.emplace_back([&]() {
      std::vector<int> my_popped;
      int prev_x = -1;
      int x;
      while (q.TryPop(&x)) {
        // Make sure the ints are popped off in order.
        EXPECT_LT(prev_x, x);
        prev_x = x;
        my_popped.push_back(x);
        // Sleep a little to give other threads a chance.
        absl::SleepFor(absl::Microseconds(1));
      }

      // Push all the ints we popped off onto a shared set.
      absl::MutexLock lock(&m);
      std::cerr << "popped " << my_popped.size() << " ints " << std::endl;
      for (int x : my_popped) {
        popped[x] += 1;
      }
    });
  }

  // Wait for all threads to finish.
  for (auto& t : threads) {
    t.join();
  }

  // Check that the threads popped exactly the ints pushed.
  EXPECT_THAT(popped, ::testing::ContainerEq(pushed));
}

}  // namespace
}  // namespace minigo
