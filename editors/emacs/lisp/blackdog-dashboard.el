;;; blackdog-dashboard.el --- Dashboard for Blackdog -*- lexical-binding: t; -*-

;; Copyright (C) 2026

;;; Commentary:

;; Magit-style Blackdog dashboard backed by `blackdog snapshot`.

;;; Code:

(require 'blackdog-core)
(require 'blackdog-results)
(require 'blackdog-search)
(require 'blackdog-task)
(require 'magit-section)
(require 'subr-x)

(defvar blackdog-dashboard-mode-map
  (let ((map (copy-keymap magit-section-mode-map)))
    (define-key map (kbd "g") #'blackdog-dashboard-refresh)
    (define-key map (kbd "q") #'quit-window)
    (define-key map (kbd "RET") #'blackdog-dashboard-visit)
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
         (snapshot (blackdog-snapshot root t))
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
         (snapshot (blackdog-snapshot root t))
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
        (highlights (alist-get 'hero_highlights snapshot)))
    (insert (format "%s\n%s\n\n" project root))
    (dolist (pair `(("Branch" . ,(alist-get 'branch highlights))
                    ("Commit" . ,(alist-get 'commit highlights))
                    ("Latest Run" . ,(alist-get 'latest_run highlights))
                    ("Completed Time" . ,(alist-get 'completed_task_time highlights))
                    ("Average Task" . ,(alist-get 'average_completed_task_time highlights))))
      (when (cdr pair)
        (insert (format "%-14s %s\n" (car pair) (cdr pair)))))
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
  (magit-insert-section (tasks)
    (magit-insert-heading "Board Tasks")
    (if-let ((tasks (alist-get 'board_tasks snapshot)))
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
            (when-let ((preview (or (alist-get 'latest_result_preview task)
                                    (alist-get 'safe_first_slice task))))
              (insert (format "  %s\n" preview)))))
      (insert "No active board tasks.\n"))
    (insert "\n")))

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
