;;; blackdog-magit.el --- Magit integration for Blackdog -*- lexical-binding: t; -*-

;; Copyright (C) 2026

;;; Commentary:

;; Worktree-aware Magit helpers for Blackdog tasks.

;;; Code:

(require 'blackdog-core)
(require 'cl-lib)
(require 'subr-x)

(declare-function magit-diff-range "magit-diff" (range &optional args files))
(declare-function magit-status "magit-status" (directory))

(defun blackdog-magit-status-task (task &optional root)
  "Open Magit status for TASK under ROOT."
  (interactive (list (blackdog-read-task)))
  (unless (fboundp 'magit-status)
    (user-error "Magit is not available"))
  (let* ((root (or root (blackdog-project-root)))
         (worktree (or (blackdog-magit--resolve-task-worktree task root)
                       root)))
    (magit-status worktree)))

(defun blackdog-magit-diff-task (task &optional root)
  "Open a Magit diff for TASK under ROOT.

Fallback to the saved diff artifact when the task branch is no longer present."
  (interactive (list (blackdog-read-task)))
  (let* ((root (or root (blackdog-project-root)))
         (task-branch (alist-get 'task_branch task))
         (target-branch (alist-get 'target_branch task))
         (worktree (or (blackdog-magit--resolve-task-worktree task root)
                       root))
         (diff-href (alist-get 'diff_href task)))
    (cond
     ((and task-branch
           target-branch
           (fboundp 'magit-diff-range)
           (blackdog-magit--branch-exists-p root task-branch)
           (blackdog-magit--branch-exists-p root target-branch))
      (let ((default-directory worktree))
        (magit-diff-range (format "%s..%s" target-branch task-branch))))
     (diff-href
      (blackdog-open-href diff-href nil root t))
     (t
      (user-error "No live branch or saved diff is available for %s"
                  (alist-get 'id task))))))

(defun blackdog-magit--branch-exists-p (root branch)
  "Return non-nil when BRANCH exists in ROOT."
  (let ((default-directory root))
    (eq 0 (process-file "git" nil nil nil "rev-parse" "--verify" "--quiet"
                        (format "refs/heads/%s" branch)))))

(defun blackdog-magit--resolve-task-worktree (task root)
  "Resolve the current worktree path for TASK under ROOT."
  (when-let ((task-branch (alist-get 'task_branch task)))
    (alist-get task-branch
               (blackdog-magit--worktree-branches root)
               nil nil #'string=)))

(defun blackdog-magit--worktree-branches (root)
  "Return an alist of task branch to worktree path for ROOT."
  (let* ((default-directory root)
         (output (with-temp-buffer
                   (if (zerop (process-file "git" nil t nil "worktree" "list" "--porcelain"))
                       (buffer-string)
                     ""))))
    (blackdog-magit--parse-worktrees output)))

(defun blackdog-magit--parse-worktrees (output)
  "Parse git worktree porcelain OUTPUT into an alist."
  (let ((lines (split-string output "\n" t))
        current-path
        pairs)
    (dolist (line lines (nreverse pairs))
      (cond
       ((string-prefix-p "worktree " line)
        (setq current-path (string-remove-prefix "worktree " line)))
       ((and current-path (string-prefix-p "branch refs/heads/" line))
        (push (cons (string-remove-prefix "branch refs/heads/" line)
                    current-path)
              pairs))))))

(provide 'blackdog-magit)

;;; blackdog-magit.el ends here
