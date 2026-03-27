;;; blackdog-task.el --- Task reader for Blackdog -*- lexical-binding: t; -*-

;; Copyright (C) 2026

;;; Commentary:

;; Read-only task detail buffer backed by `blackdog snapshot`.

;;; Code:

(require 'blackdog-core)
(require 'button)
(require 'subr-x)

(declare-function blackdog-magit-diff-task "blackdog-magit" (task &optional root))
(declare-function blackdog-magit-status-task "blackdog-magit" (task &optional root))

(defvar-local blackdog-task-id nil)

(defvar blackdog-task-view-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map special-mode-map)
    (define-key map (kbd "g") #'blackdog-task-view-refresh)
    (define-key map (kbd "RET") #'push-button)
    (define-key map (kbd "m") #'blackdog-task-view-magit-status)
    (define-key map (kbd "d") #'blackdog-task-view-magit-diff)
    map)
  "Keymap for `blackdog-task-view-mode'.")

(define-derived-mode blackdog-task-view-mode special-mode "Blackdog-Task"
  "Read-only task buffer for Blackdog."
  (setq-local truncate-lines nil))

(defun blackdog-task-view (task &optional root)
  "Open TASK from ROOT in a dedicated reader buffer."
  (interactive (list (blackdog-read-task)))
  (let* ((root (or root (blackdog-project-root)))
         (task-id (alist-get 'id task))
         (buffer (get-buffer-create
                  (format "*Blackdog Task: %s*" task-id))))
    (with-current-buffer buffer
      (blackdog-task-view-mode)
      (setq-local blackdog-buffer-root root)
      (setq-local blackdog-task-id task-id)
      (setq-local blackdog-refresh-function #'blackdog-task-view-refresh)
      (blackdog-task-view-refresh))
    (pop-to-buffer buffer)))

(defun blackdog-task-view-refresh ()
  "Refresh the current task view."
  (interactive)
  (let* ((root (or blackdog-buffer-root (blackdog-project-root)))
         (snapshot (blackdog-snapshot root t))
         (task (blackdog-task-by-id blackdog-task-id snapshot)))
    (unless task
      (user-error "Task %s is no longer present" blackdog-task-id))
    (let ((inhibit-read-only t))
      (erase-buffer)
      (setq-local blackdog-buffer-root root)
      (insert (format "%s  %s\n\n"
                      (alist-get 'id task)
                      (alist-get 'title task)))
      (blackdog-task--insert-pairs
       `(("Status" . ,(or (alist-get 'operator_status task)
                          (alist-get 'status task)))
         ("Objective" . ,(or (alist-get 'objective_title task)
                             (alist-get 'objective task)
                             ""))
         ("Lane" . ,(or (alist-get 'lane_title task) ""))
         ("Wave" . ,(format "%s" (or (alist-get 'wave task) "")))
         ("Priority" . ,(or (alist-get 'priority task) ""))
         ("Risk" . ,(or (alist-get 'risk task) ""))
         ("Branch" . ,(or (alist-get 'task_branch task) ""))
         ("Target" . ,(or (alist-get 'target_branch task) ""))
         ("Latest Result" . ,(or (alist-get 'latest_result_status task) ""))))
      (blackdog-task--insert-section "Safe First Slice"
        (or (alist-get 'safe_first_slice task) ""))
      (blackdog-task--insert-section "Why"
        (or (alist-get 'why task) ""))
      (blackdog-task--insert-section "Latest Result Preview"
        (or (alist-get 'latest_result_preview task) ""))
      (blackdog-task--insert-list "What Changed"
                                  (alist-get 'latest_result_what_changed task))
      (blackdog-task--insert-list "Validation"
                                  (alist-get 'latest_result_validation task))
      (blackdog-task--insert-list "Residual"
                                  (alist-get 'latest_result_residual task))
      (blackdog-task--insert-path-list "Paths"
                                       (alist-get 'paths task)
                                       root)
      (blackdog-task--insert-list "Checks"
                                  (alist-get 'checks task))
      (blackdog-task--insert-list "Docs"
                                  (alist-get 'docs task))
      (blackdog-task--insert-link-list "Artifacts"
                                       (alist-get 'links task)
                                       snapshot
                                       root)
      (blackdog-task--insert-activity "Activity"
                                      (alist-get 'activity task))
      (goto-char (point-min)))))

(defun blackdog-task-view-magit-status ()
  "Open Magit status for the current task."
  (interactive)
  (require 'blackdog-magit)
  (let ((task (blackdog-task-by-id blackdog-task-id nil blackdog-buffer-root)))
    (blackdog-magit-status-task task blackdog-buffer-root)))

(defun blackdog-task-view-magit-diff ()
  "Open a Magit diff for the current task."
  (interactive)
  (require 'blackdog-magit)
  (let ((task (blackdog-task-by-id blackdog-task-id nil blackdog-buffer-root)))
    (blackdog-magit-diff-task task blackdog-buffer-root)))

(defun blackdog-task--insert-pairs (pairs)
  "Insert PAIRS as aligned key/value rows."
  (dolist (pair pairs)
    (when (and (cdr pair) (not (string-empty-p (format "%s" (cdr pair)))))
      (insert (format "%-14s %s\n" (car pair) (cdr pair)))))
  (insert "\n"))

(defmacro blackdog-task--insert-section (title content)
  "Insert TITLE and CONTENT when CONTENT is non-empty."
  `(let ((value ,content))
     (when (and value (not (string-empty-p (format "%s" value))))
       (insert (format "%s\n%s\n\n" ,title value)))))

(defun blackdog-task--insert-list (title items)
  "Insert TITLE and ITEMS when ITEMS is non-empty."
  (when items
    (insert (format "%s\n" title))
    (dolist (item items)
      (insert (format "- %s\n" item)))
    (insert "\n")))

(defun blackdog-task--insert-path-list (title paths root)
  "Insert TITLE with clickable project PATHS under ROOT."
  (when paths
    (insert (format "%s\n" title))
    (dolist (path paths)
      (insert "- ")
      (insert-text-button
       path
       'follow-link t
       'action (lambda (_button)
                 (blackdog-open-project-path path root t)))
      (insert "\n"))
    (insert "\n")))

(defun blackdog-task--insert-link-list (title links snapshot root)
  "Insert TITLE with clickable artifact LINKS from SNAPSHOT and ROOT."
  (when links
    (insert (format "%s\n" title))
    (dolist (link links)
      (let ((label (alist-get 'label link))
            (href (alist-get 'href link)))
        (insert "- ")
        (insert-text-button
         label
         'follow-link t
         'action (lambda (_button)
                   (blackdog-open-href href snapshot root t)))
        (insert "\n")))
    (insert "\n")))

(defun blackdog-task--insert-activity (title activity)
  "Insert TITLE with ACTIVITY rows."
  (when activity
    (insert (format "%s\n" title))
    (dolist (row activity)
      (insert (format "- %s  %s  %s\n"
                      (or (alist-get 'at row) "")
                      (or (alist-get 'actor row) "")
                      (or (alist-get 'message row) ""))))
    (insert "\n")))

(provide 'blackdog-task)

;;; blackdog-task.el ends here
