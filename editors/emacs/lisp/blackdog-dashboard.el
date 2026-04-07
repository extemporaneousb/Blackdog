;;; blackdog-dashboard.el --- Dashboard for Blackdog -*- lexical-binding: t; -*-

;; Copyright (C) 2026

;;; Commentary:

;; Magit-style Blackdog dashboard backed by `blackdog snapshot`.

;;; Code:

(require 'button)
(require 'cl-lib)
(require 'blackdog-core)
(require 'blackdog-results)
(require 'blackdog-search)
(require 'blackdog-task)
(require 'magit-section)
(require 'subr-x)
(require 'time-date)

(declare-function blackdog-chat "blackdog" (&optional root))
(declare-function blackdog-chat-history "blackdog" (&optional root include-all))
(declare-function blackdog-open-snapshot-stats "blackdog" (&optional root))
(declare-function blackdog-open-unattended-tuning "blackdog" (&optional root))
(declare-function blackdog-telemetry-open "blackdog-telemetry" (&optional root actor))
(declare-function blackdog-codex-open-session "blackdog-codex" (session &optional root))
(declare-function blackdog-codex-session-list "blackdog-codex" (&optional root include-all))

(defconst blackdog-dashboard--failed-result-statuses
  '("blocked" "error" "failed" "partial")
  "Latest result statuses that count as a failed completion bucket.")

(defconst blackdog-dashboard--shown-completed-limit 60
  "Maximum number of completed tasks to render in the dashboard.")

(defun blackdog-dashboard--time-seconds (timestamp)
  "Return TIMESTAMP as seconds since the epoch."
  (when (and (stringp timestamp) (not (string-empty-p timestamp)))
    (float-time (date-to-time timestamp))))

(defun blackdog-dashboard--age-label (timestamp &optional now)
  "Return a relative age label for TIMESTAMP using NOW."
  (let ((then (blackdog-dashboard--time-seconds timestamp))
        (now-seconds (float-time (or now (current-time)))))
    (when (and then (<= then now-seconds))
      (concat
       (blackdog-format-duration-seconds
        (floor (- now-seconds then)))
       " ago"))))

(defun blackdog-dashboard--completion-stamp (task)
  "Return the best completion timestamp for TASK."
  (or (alist-get 'completed_at task)
      (alist-get 'latest_result_at task)
      (alist-get 'latest_run_at task)))

(defun blackdog-dashboard--task-bucket (task)
  "Return the dashboard bucket symbol for TASK."
  (let ((task-status (string-trim (or (alist-get 'status task) "")))
        (run-status (string-trim (or (alist-get 'latest_run_status task) "")))
        (result-status (downcase (string-trim (or (alist-get 'latest_result_status task) "")))))
    (cond
     ((string= task-status "done")
      (if (member result-status blackdog-dashboard--failed-result-statuses)
          'complete-failed
        'complete-succeeded))
     ((string= run-status "running")
      'running)
     ((string= task-status "claimed")
      'claimed)
     (t
      'submitted))))

(defun blackdog-dashboard--bucket-sort-key (task bucket)
  "Return a numeric sort key for TASK in BUCKET."
  (pcase bucket
    ('running (- (or (alist-get 'run_elapsed_seconds task) 0)))
    ('claimed (or (blackdog-dashboard--time-seconds (alist-get 'claimed_at task))
                  most-positive-fixnum))
    ('complete-failed (- (or (blackdog-dashboard--time-seconds
                              (blackdog-dashboard--completion-stamp task))
                             0)))
    ('complete-succeeded (- (or (blackdog-dashboard--time-seconds
                                 (blackdog-dashboard--completion-stamp task))
                                0)))
    (_ (or (blackdog-dashboard--time-seconds (alist-get 'created_at task))
           most-positive-fixnum))))

(defun blackdog-dashboard--bucket-tasks (tasks bucket)
  "Return TASKS that belong to BUCKET, sorted for dashboard display."
  (seq-sort-by (lambda (task)
                 (blackdog-dashboard--bucket-sort-key task bucket))
               #'<
               (seq-filter (lambda (task)
                             (eq (blackdog-dashboard--task-bucket task) bucket))
                           (or tasks '()))))

(defun blackdog-dashboard--completed-tasks (tasks)
  "Return completed TASKS sorted newest-first."
  (seq-sort-by (lambda (task)
                 (- (or (blackdog-dashboard--time-seconds
                         (blackdog-dashboard--completion-stamp task))
                        0)))
               #'<
               (seq-filter (lambda (task)
                             (memq (blackdog-dashboard--task-bucket task)
                                   '(complete-failed complete-succeeded)))
                           (or tasks '()))))

(defun blackdog-dashboard--task-timing-line (task &optional now)
  "Return a compact lifecycle timing summary for TASK using NOW."
  (let ((timings nil)
        (submitted (blackdog-dashboard--age-label (alist-get 'created_at task) now))
        (claimed (blackdog-dashboard--age-label (alist-get 'claimed_at task) now))
        (completed (blackdog-dashboard--age-label
                    (blackdog-dashboard--completion-stamp task)
                    now))
        (running (or (alist-get 'run_elapsed_label task)
                     (when-let ((seconds (alist-get 'run_elapsed_seconds task)))
                       (blackdog-format-duration-seconds seconds))))
        (commit (or (alist-get 'task_commit_short task)
                    (alist-get 'landed_commit_short task))))
    (when submitted
      (push (format "Submitted: %s" submitted) timings))
    (when claimed
      (push (format "Claimed: %s" claimed) timings))
    (when running
      (push (format "Running: %s" running) timings))
    (when completed
      (push (format "Completed: %s" completed) timings))
    (when commit
      (push (format "Commit: %s" commit) timings))
    (when timings
      (string-join (nreverse timings) "  "))))

(defvar blackdog-dashboard-mode-map
  (let ((map (copy-keymap magit-section-mode-map)))
    (define-key map (kbd "g") #'blackdog-refresh)
    (define-key map (kbd "q") #'quit-window)
    (define-key map (kbd "RET") #'blackdog-dashboard-visit)
    (define-key map (kbd "n") #'blackdog-chat)
    (define-key map (kbd "h") #'blackdog-chat-history)
    (define-key map (kbd "v") #'blackdog-telemetry-open)
    (define-key map (kbd "V") #'blackdog-open-snapshot-stats)
    (define-key map (kbd "U") #'blackdog-open-unattended-tuning)
    (define-key map (kbd "r") #'blackdog-results-open)
    (define-key map (kbd "s") #'blackdog-find-task)
    map)
  "Keymap for `blackdog-dashboard-mode'.")

(define-derived-mode blackdog-dashboard-mode magit-section-mode "Blackdog"
  "Major mode for the Blackdog dashboard."
  (setq-local truncate-lines t))

(defun blackdog-dashboard-open (&optional root)
  "Open the Blackdog dashboard for ROOT."
  (interactive)
  (let* ((root (or root (blackdog-project-root)))
         (snapshot (blackdog-snapshot root))
         (buffer (get-buffer-create
                  (format "*Blackdog: %s*"
                          (alist-get 'project_name snapshot)))))
    (with-current-buffer buffer
      (blackdog-dashboard-mode)
      (setq-local blackdog-buffer-root root)
      (setq-local blackdog-refresh-function #'blackdog-dashboard-refresh)
      (blackdog-dashboard-refresh))
    (pop-to-buffer buffer)))

(defun blackdog-dashboard-refresh ()
  "Refresh the current dashboard buffer."
  (interactive)
  (let* ((root (or blackdog-buffer-root (blackdog-project-root)))
         (snapshot (blackdog-snapshot root))
         (inhibit-read-only t))
    (erase-buffer)
    (setq-local blackdog-buffer-root root)
    (magit-insert-section (dashboard)
      (blackdog-dashboard--insert-hero snapshot)
      (blackdog-dashboard--insert-overview snapshot)
      (blackdog-dashboard--insert-objectives snapshot)
      (blackdog-dashboard--insert-tasks snapshot)
      (blackdog-dashboard--insert-recent-results snapshot))
    (goto-char (point-min))))

(defun blackdog-dashboard-visit ()
  "Visit the section at point."
  (interactive)
  (let ((section (magit-current-section)))
    (pcase (and section (oref section type))
      ('blackdog-task
       (blackdog-task-view (oref section value) blackdog-buffer-root))
      ('blackdog-result
       (let* ((row (oref section value))
              (task-id (alist-get 'task_id row)))
         (blackdog-task-view
          (blackdog-task-by-id task-id nil blackdog-buffer-root)
          blackdog-buffer-root)))
      (_ (when section
           (magit-section-toggle section))))))

(defun blackdog-dashboard--insert-hero (snapshot)
  "Insert the hero banner from SNAPSHOT."
  (let ((project (alist-get 'project_name snapshot))
        (root (alist-get 'project_root snapshot))
        (highlights (alist-get 'hero_highlights snapshot))
        (latest-session (car (blackdog-codex-session-list blackdog-buffer-root nil))))
    (insert (format "%s\n%s\n\n" project root))
    (dolist (pair `(("Branch" . ,(alist-get 'branch highlights))
                    ("Commit" . ,(alist-get 'commit highlights))
                    ("Latest Run" . ,(alist-get 'latest_run highlights))
                    ("Completed Time" . ,(alist-get 'completed_task_time highlights))
                    ("Average Task" . ,(alist-get 'average_completed_task_time highlights))))
      (when (cdr pair)
        (insert (format "%-14s %s\n" (car pair) (cdr pair)))))
    (insert "Cockpit        ")
    (insert-text-button
     "New Chat"
     'follow-link t
     'action (lambda (_button)
               (blackdog-chat blackdog-buffer-root)))
    (insert "  ")
    (insert-text-button
     "Chat History"
     'follow-link t
     'action (lambda (_button)
               (blackdog-chat-history blackdog-buffer-root)))
    (insert "  ")
    (insert-text-button
     "Supervisor Monitor"
     'follow-link t
     'action (lambda (_button)
               (blackdog-telemetry-open blackdog-buffer-root)))
    (insert "  ")
    (insert-text-button
     "Snapshot Stats"
     'follow-link t
     'action (lambda (_button)
               (blackdog-open-snapshot-stats blackdog-buffer-root)))
    (insert "  ")
    (insert-text-button
     "Unattended Tuning"
     'follow-link t
     'action (lambda (_button)
               (blackdog-open-unattended-tuning blackdog-buffer-root)))
    (insert "\n")
    (when latest-session
      (insert "Latest Chat     ")
      (insert-text-button
       (format "%s  %s"
               (or (alist-get 'updated_at latest-session) "")
               (or (alist-get 'title latest-session) ""))
       'follow-link t
       'action (lambda (_button)
                 (blackdog-codex-open-session latest-session blackdog-buffer-root)))
      (insert "\n"))
    (insert "\n")))

(defun blackdog-dashboard--insert-overview (snapshot)
  "Insert queue overview data from SNAPSHOT."
  (magit-insert-section (overview)
    (magit-insert-heading "Overview")
    (let ((counts (alist-get 'counts snapshot))
          (queue (alist-get 'queue_status snapshot)))
      (insert (format "Ready: %s  Claimed: %s  Waiting: %s  Done: %s\n"
                      (alist-get 'ready counts)
                      (alist-get 'claimed counts)
                      (alist-get 'waiting counts)
                      (alist-get 'done counts)))
      (insert (format "Running: %s  Blocked: %s  Completed Today: %s  Total: %s\n"
                      (alist-get 'running queue)
                      (alist-get 'blocked queue)
                      (alist-get 'completed_today queue)
                      (alist-get 'completed_all_time queue)))
      (insert "\n"))))

(defun blackdog-dashboard--insert-objectives (snapshot)
  "Insert objective summaries from SNAPSHOT."
  (magit-insert-section (objectives)
    (magit-insert-heading "Objectives")
    (if-let ((objectives (alist-get 'objectives snapshot)))
        (dolist (row objectives)
          (insert (format "- [%s/%s] %s\n"
                          (alist-get 'done row)
                          (alist-get 'total row)
                          (alist-get 'title row))))
      (insert "No objectives recorded.\n"))
    (insert "\n")))

(defun blackdog-dashboard--insert-tasks (snapshot)
  "Insert board tasks from SNAPSHOT."
  (let* ((all-tasks (alist-get 'tasks snapshot))
         (board-tasks (or (alist-get 'board_tasks snapshot)
                          (seq-filter (lambda (task)
                                        (alist-get 'lane_id task))
                                      all-tasks)))
         (submitted (blackdog-dashboard--bucket-tasks board-tasks 'submitted))
         (claimed (blackdog-dashboard--bucket-tasks board-tasks 'claimed))
         (running (blackdog-dashboard--bucket-tasks board-tasks 'running))
         (completed (blackdog-dashboard--completed-tasks all-tasks))
         (visible-completed (seq-take completed
                                      blackdog-dashboard--shown-completed-limit))
         (failed-visible (seq-filter (lambda (task)
                                       (eq (blackdog-dashboard--task-bucket task)
                                           'complete-failed))
                                     visible-completed))
         (succeeded-visible (seq-filter (lambda (task)
                                          (eq (blackdog-dashboard--task-bucket task)
                                              'complete-succeeded))
                                        visible-completed))
         (now (current-time)))
  (magit-insert-section (tasks)
    (magit-insert-heading "Board Tasks")
    (blackdog-dashboard--insert-task-bucket "Submitted" submitted now)
    (blackdog-dashboard--insert-task-bucket "Claimed" claimed now)
    (blackdog-dashboard--insert-task-bucket "Running" running now)
    (magit-insert-section (completed-tasks)
      (magit-insert-heading
        (format "Complete (%s shown of %s)"
                (length visible-completed)
                (length completed)))
      (blackdog-dashboard--insert-task-bucket "Failed" failed-visible now)
      (blackdog-dashboard--insert-task-bucket "Succeeded" succeeded-visible now)))
    (insert "\n")))

(defun blackdog-dashboard--insert-task-bucket (label tasks now)
  "Insert one task bucket with LABEL for TASKS using NOW."
  (magit-insert-section (task-bucket label)
    (magit-insert-heading (format "%s (%s)" label (length tasks)))
    (if tasks
        (dolist (task tasks)
          (magit-insert-section (blackdog-task task)
            (magit-insert-heading
              (format "[%s] %s  %s"
                      (or (alist-get 'operator_status task)
                          (alist-get 'status task)
                          "?")
                      (alist-get 'id task)
                      (alist-get 'title task)))
            (insert (format "  Lane: %s  Wave: %s  Priority: %s\n"
                            (or (alist-get 'lane_title task) "")
                            (or (alist-get 'wave task) "")
                            (or (alist-get 'priority task) "")))
            (when-let ((timings (blackdog-dashboard--task-timing-line task now)))
              (insert (format "  %s\n" timings)))
            (when-let ((preview (or (alist-get 'latest_result_preview task)
                                    (alist-get 'safe_first_slice task))))
              (insert (format "  %s\n" preview)))))
      (insert (format "No %s tasks.\n" (downcase label))))))

(defun blackdog-dashboard--insert-recent-results (snapshot)
  "Insert recent results from SNAPSHOT."
  (magit-insert-section (results)
    (magit-insert-heading "Recent Results")
    (if-let ((results (alist-get 'recent_results snapshot)))
        (dolist (row results)
          (magit-insert-section (blackdog-result row)
            (magit-insert-heading
              (format "[%s] %s  %s"
                      (alist-get 'status row)
                      (alist-get 'task_id row)
                      (or (alist-get 'preview row) "")))
            (insert (format "  Actor: %s  At: %s\n"
                            (or (alist-get 'actor row) "")
                            (or (alist-get 'recorded_at row) "")))))
      (insert "No recent results.\n"))
    (insert "\n")))

(provide 'blackdog-dashboard)

;;; blackdog-dashboard.el ends here
