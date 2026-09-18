"use strict";
// Shows/hides the ClassGroup admin's tutor <select> slots (see
// TutorSlotsWidget in schedule/admin.py) to match the "number of tutors"
// field's value - never hiding a slot that already holds a value, so
// lowering the count never silently discards a picked tutor until the
// admin explicitly clears that slot themselves.
document.addEventListener("DOMContentLoaded", function () {
  var countInput = document.getElementById("id_tutor_count");
  var slots = document.querySelectorAll(".tutor-slot-select");
  if (!countInput || !slots.length) {
    return;
  }

  function filledCount() {
    var filled = 0;
    slots.forEach(function (select, index) {
      if (select.value) {
        filled = index + 1;
      }
    });
    return filled;
  }

  function sync(clearHidden) {
    var wanted = Math.max(1, Math.min(slots.length, parseInt(countInput.value, 10) || 1));
    var visibleCount = Math.max(wanted, filledCount());
    slots.forEach(function (select, index) {
      var visible = index < visibleCount;
      select.hidden = !visible;
      if (!visible && clearHidden) {
        select.value = "";
      }
    });
  }

  countInput.addEventListener("input", function () {
    sync(true);
  });
  sync(false);
});
