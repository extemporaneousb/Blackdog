;;; blackdog-task.el --- Task reader for Blackdog -*- lexical-binding: t; -*-

;; Copyright (C) 2026

;;; Commentary:

;; Read-only task detail buffer backed by `blackdog snapshot`.

;;; Code:

(require 'blackdog-core)
(require 'blackdog-artifacts)
(require 'button)
(require 'subr-x)

(declare-function blackdog-magit-diff-task "blackdog-magit" (task &optional root))
(declare-function blackdog-magit-status-task "blackdog-magit" (task &optional root))
(declare-function blackdog-read-thread "blackdog-thread" (&optional prompt root task-id))
(declare-function blackdog-thread-view "blackdog-thread" (thread &optional root))

(defvar-local blackdog-task-id nil)
(defvar-local blackdog-task-data nil
  "Task alist currently rendered in `blackdog-task-view'.")
(defvar-local blackdog-task-artifact-kind nil
  "Artifact kind rendered in the current Blackdog artifact buffer.")

(defvar blackdog-task-view-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map special-mode-map)
    (define-key map (kbd "g") #'blackdog-refresh)
    (define-key map (kbd "RET") #'push-button)
    (define-key map (kbd "c") #'blackdog-task-claim)
    (define-key map (kbd "w") #'blackdog-task-launch)
    (define-key map (kbd "l") #'blackdog-task-release)
    (define-key map (kbd "e") #'blackdog-task-complete)
    (define-key map (kbd "k") #'blackdog-task-remove)
    (define-key map (kbd "m") #'blackdog-task-view-magit-status)
    (define-key map (kbd "d") #'blackdog-task-view-magit-diff)
    (define-key map (kbd "h") #'blackdog-task-open-conversation)
    (define-key map (kbd "p") #'blackdog-task-view-browse-prompt)
    (define-key map (kbd "t") #'blackdog-task-view-browse-thread)
    (define-key map (kbd "P") #'blackdog-task-view-open-prompt)
    (define-key map (kbd "r") #'blackdog-task-view-open-result)
    (define-key map (kbd "O") #'blackdog-task-open-stdout)
    (define-key map (kbd "E") #'blackdog-task-open-stderr)
    (define-key map (kbd "D") #'blackdog-task-open-diff)
    (define-key map (kbd "M") #'blackdog-task-open-metadata)
    (define-key map (kbd "F") #'blackdog-task-open-result)
    (define-key map (kbd "R") #'blackdog-task-open-run)
    map)
  "Keymap for `blackdog-task-view-mode'.")

(define-derived-mode blackdog-task-view-mode special-mode "Blackdog-Task"
  "Read-only task buffer for Blackdog."
  (setq-local truncate-lines nil))

(defvar blackdog-task-artifact-view-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map special-mode-map)
    (define-key map (kbd "g") #'blackdog-refresh)
    (define-key map (kbd "RET") #'push-button)
    (define-key map (kbd "p") #'blackdog-task-view-browse-prompt)
    (define-key map (kbd "t") #'blackdog-task-view-browse-thread)
    (define-key map (kbd "o") #'blackdog-task-artifact-view-open-source)
    (define-key map (kbd "m") #'blackdog-task-open-metadata)
    (define-key map (kbd "r") #'blackdog-task-open-run)
    map)
  "Keymap for `blackdog-task-artifact-view-mode'.")

(define-derived-mode blackdog-task-artifact-view-mode special-mode "Blackdog-Artifact"
  "Read-only Blackdog prompt/thread browser."
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
         (snapshot (blackdog-snapshot root))
         (task (blackdog-task-by-id blackdog-task-id snapshot)))
    (unless task
      (user-error "Task %s is no longer present" blackdog-task-id))
    (let ((inhibit-read-only t))
      (erase-buffer)
      (setq-local blackdog-buffer-root root)
      (setq-local blackdog-task-data task)
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
      (blackdog-task--insert-lifecycle-actions task)
      (blackdog-task--insert-quick-links task)
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
      (blackdog-task--insert-conversation-links task root)
      (blackdog-task--insert-artifact-links "Artifacts" task)
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

(defun blackdog-task-browse-artifact (artifact &optional task root)
  "Open a read-only browser for TASK ARTIFACT from ROOT.

ARTIFACT should be `prompt' or `thread'."
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (task (blackdog-task--task-for-action task))
         (task-id (alist-get 'id task))
         (buffer (get-buffer-create
                  (format "*Blackdog %s: %s*"
                          (capitalize (symbol-name artifact))
                          task-id))))
    (unless task
      (user-error "No task selected"))
    (with-current-buffer buffer
      (blackdog-task-artifact-view-mode)
      (setq-local blackdog-buffer-root root)
      (setq-local blackdog-task-id task-id)
      (setq-local blackdog-task-artifact-kind artifact)
      (setq-local blackdog-refresh-function #'blackdog-task-artifact-view-refresh)
      (blackdog-task-artifact-view-refresh))
    (pop-to-buffer buffer)
    buffer))

(defun blackdog-task--artifact-kind-title (artifact)
  "Return a display title for ARTIFACT."
  (capitalize (symbol-name artifact)))

(defun blackdog-task--artifact-source-label (task artifact)
  "Return a display label for TASK ARTIFACT source."
  (if (eq artifact 'thread)
      (let ((thread-href (blackdog-task-artifact-href task 'thread)))
        (cond
         ((equal thread-href (blackdog-task-artifact-href task 'stderr)) "Stderr")
         ((equal thread-href (blackdog-task-artifact-href task 'stdout)) "Stdout")
         (t "Thread")))
    (blackdog-task--artifact-kind-title artifact)))

(defun blackdog-task--artifact-source-path (task artifact root snapshot)
  "Return the resolved local path for TASK ARTIFACT under ROOT and SNAPSHOT."
  (let ((target (blackdog-resolve-href
                 (blackdog-task-artifact-href task artifact)
                 snapshot
                 root)))
    (unless target
      (user-error "No %s artifact for task %s"
                  artifact
                  (alist-get 'id task)))
    (when (string-match-p "\\`https?://" target)
      (user-error "%s browser only supports local artifacts" artifact))
    target))

(defun blackdog-task--insert-action-button (label action)
  "Insert LABEL button bound to ACTION on the current line."
  (insert "- ")
  (insert-text-button label 'follow-link t 'action action)
  (insert "\n"))

(defun blackdog-task--insert-artifact-browser-actions (task artifact)
  "Insert browser action links for TASK ARTIFACT."
  (let ((prompt-href (blackdog-task-artifact-href task 'prompt))
        (thread-href (blackdog-task-artifact-href task 'thread))
        (metadata-href (blackdog-task-artifact-href task 'metadata))
        (run-href (blackdog-task-artifact-href task 'run)))
    (insert "Actions\n")
    (when prompt-href
      (blackdog-task--insert-action-button
       "Browse Prompt"
       (lambda (_button)
         (blackdog-task-view-browse-prompt task))))
    (when thread-href
      (blackdog-task--insert-action-button
       "Browse Thread"
       (lambda (_button)
         (blackdog-task-view-browse-thread task))))
    (blackdog-task--insert-action-button
     "Open Source"
     (lambda (_button)
       (blackdog-task-open-artifact artifact task nil t)))
    (when metadata-href
      (blackdog-task--insert-action-button
       "Open Metadata"
       (lambda (_button)
         (blackdog-task-open-metadata task))))
    (when run-href
      (blackdog-task--insert-action-button
       "Open Run"
       (lambda (_button)
         (blackdog-task-open-run task))))
    (insert "\n")))

(defun blackdog-task-artifact-view-refresh ()
  "Refresh the current prompt/thread browser buffer."
  (interactive)
  (let* ((root (or blackdog-buffer-root (blackdog-project-root)))
         (snapshot (blackdog-snapshot root))
         (task (blackdog-task-by-id blackdog-task-id snapshot))
         (artifact blackdog-task-artifact-kind)
         (href (and task (blackdog-task-artifact-href task artifact))))
    (unless task
      (user-error "Task %s is no longer present" blackdog-task-id))
    (unless href
      (user-error "Task %s has no %s artifact" blackdog-task-id artifact))
    (let* ((source-path (blackdog-task--artifact-source-path task artifact root snapshot))
           (inhibit-read-only t))
      (erase-buffer)
      (setq-local blackdog-buffer-root root)
      (setq-local blackdog-task-data task)
      (insert (format "%s  %s\n\n"
                      (alist-get 'id task)
                      (blackdog-task--artifact-kind-title artifact)))
      (blackdog-task--insert-pairs
       `(("Task" . ,(alist-get 'title task))
         ("Source Artifact" . ,(blackdog-task--artifact-source-label task artifact))
         ("Source" . ,href)
         ("Run" . ,(or (blackdog-task-artifact-href task 'run) ""))))
      (blackdog-task--insert-artifact-browser-actions task artifact)
      (insert (format "%s\n" (blackdog-task--artifact-kind-title artifact)))
      (insert-file-contents source-path)
      (goto-char (point-min)))))

(defun blackdog-task-artifact-view-open-source ()
  "Open the raw artifact source for the current prompt/thread browser."
  (interactive)
  (let ((task (blackdog-task--task-for-action)))
    (unless task
      (user-error "No task selected"))
    (blackdog-task-open-artifact blackdog-task-artifact-kind task nil t)))

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

(defun blackdog-task--insert-lifecycle-actions (task)
  "Insert task lifecycle action buttons for TASK."
  (insert "Actions\n")
  (dolist (row `(("Claim" . ,#'blackdog-task-claim)
                 ("Launch Worktree" . ,#'blackdog-task-launch)
                 ("Release" . ,#'blackdog-task-release)
                 ("Complete" . ,#'blackdog-task-complete)
                 ("Remove" . ,#'blackdog-task-remove)))
    (let ((label (car row))
          (fn (cdr row)))
      (blackdog-task--insert-action-button
       label
       (lambda (_button)
         (funcall fn task)))))
  (insert "\n"))

(defun blackdog-task--insert-conversation-links (task root)
  "Insert linked conversation-thread buttons for TASK under ROOT."
  (let ((threads (alist-get 'conversation_threads task)))
    (when threads
      (insert "Conversations\n")
      (dolist (thread threads)
        (let ((thread-id (alist-get 'id thread))
              (title (alist-get 'title thread)))
          (insert "- ")
          (insert-text-button
           (format "%s  %s" thread-id title)
           'follow-link t
           'action (lambda (_button)
                     (require 'blackdog-thread)
                     (blackdog-thread-view thread root)))
          (insert "\n")))
      (insert "\n"))))

(defun blackdog-task--insert-artifact-links (title task)
  "Insert TITLE with canonical artifact links from TASK."
  (let ((links (blackdog-task-artifacts-links task)))
    (when links
      (insert (format "%s\n" title))
      (dolist (link links)
        (let* ((artifact (alist-get 'artifact link))
               (label (alist-get 'label link))
               (href (alist-get 'href link)))
          (insert "- ")
          (insert-text-button
           label
           'follow-link t
           'action (lambda (_button)
                     (if artifact
                         (blackdog-task-open-artifact artifact task nil t)
                       (blackdog-open-href href nil blackdog-buffer-root t))))
          (insert "\n")))
      (insert "\n"))))

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

(defun blackdog-task--task-for-action (&optional task)
  "Return TASK or the current task buffer's task."
  (or task
      blackdog-task-data
      (and blackdog-task-id
           (blackdog-task-by-id blackdog-task-id nil blackdog-buffer-root))))

(defun blackdog-task--command-agent (&optional label)
  "Return the agent name for one task command with LABEL."
  (if current-prefix-arg
      (blackdog-read-agent (format "%s as: " (or label "Run")))
    blackdog-default-agent))

(defun blackdog-task--refresh-after-mutation (root)
  "Clear cached state for ROOT and refresh the current buffer when possible."
  (blackdog-clear-cache root)
  (when (functionp blackdog-refresh-function)
    (funcall blackdog-refresh-function)))

(defun blackdog-task--open-worktree (path)
  "Open task worktree PATH in a useful buffer."
  (ignore-errors
    (require 'magit))
  (cond
   ((not path)
    (message "No worktree path returned"))
   ((fboundp 'magit-status)
    (magit-status path))
   (t
    (dired path))))

(defun blackdog-task-claim (&optional task root)
  "Claim TASK from ROOT for the default Emacs agent."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (task (blackdog-task--task-for-action task))
         (task-id (alist-get 'id task))
         (agent (blackdog-task--command-agent "Claim")))
    (unless task
      (user-error "No task selected"))
    (apply #'blackdog--call-json root
           (list "claim" "--agent" agent "--id" task-id))
    (blackdog-task--refresh-after-mutation root)
    (message "Claimed %s as %s" task-id agent)))

(defun blackdog-task-launch (&optional task root)
  "Claim TASK when needed, then start its WTAM worktree from ROOT."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (task (blackdog-task--task-for-action task))
         (task-id (alist-get 'id task))
         (status-key (or (alist-get 'operator_status_key task)
                         (alist-get 'status task)
                         ""))
         (claimed-by (or (alist-get 'claimed_by task) ""))
         (agent (blackdog-task--command-agent "Launch")))
    (unless task
      (user-error "No task selected"))
    (when (member status-key '("done" "complete"))
      (user-error "Task %s is already complete" task-id))
    (when (and (string= status-key "claimed")
               (not (string-empty-p claimed-by))
               (not (string= claimed-by agent)))
      (user-error "Task %s is claimed by %s" task-id claimed-by))
    (unless (string= status-key "claimed")
      (apply #'blackdog--call-json root
             (list "claim" "--agent" agent "--id" task-id)))
    (let* ((payload (apply #'blackdog--call-json root
                           (list "worktree" "start"
                                 "--actor" agent
                                 "--id" task-id
                                 "--format" "json")))
           (worktree-path (alist-get 'worktree_path payload)))
      (blackdog-task--refresh-after-mutation root)
      (blackdog-task--open-worktree worktree-path)
      (message "Started %s in %s" task-id worktree-path)
      payload)))

(defun blackdog-task-release (&optional task root)
  "Release TASK from ROOT for the default Emacs agent."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (task (blackdog-task--task-for-action task))
         (task-id (alist-get 'id task))
         (agent (blackdog-task--command-agent "Release")))
    (unless task
      (user-error "No task selected"))
    (apply #'blackdog--call root
           (list "release" "--agent" agent "--id" task-id))
    (blackdog-task--refresh-after-mutation root)
    (message "Released %s as %s" task-id agent)))

(defun blackdog-task-complete (&optional task root)
  "Complete TASK from ROOT for the default Emacs agent."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (task (blackdog-task--task-for-action task))
         (task-id (alist-get 'id task))
         (agent (blackdog-task--command-agent "Complete"))
         (note (read-string "Completion note (optional): " nil nil "")))
    (unless task
      (user-error "No task selected"))
    (apply #'blackdog--call root
           (append (list "complete" "--agent" agent "--id" task-id)
                   (unless (string-empty-p note)
                     (list "--note" note))))
    (blackdog-task--refresh-after-mutation root)
    (message "Completed %s as %s" task-id agent)))

(defun blackdog-task-remove (&optional task root)
  "Remove TASK from ROOT after confirmation."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (task (blackdog-task--task-for-action task))
         (task-id (alist-get 'id task))
         (title (alist-get 'title task))
         (actor (blackdog-task--command-agent "Remove")))
    (unless task
      (user-error "No task selected"))
    (unless (yes-or-no-p (format "Remove %s (%s)? " task-id title))
      (user-error "Task removal cancelled"))
    (apply #'blackdog--call-json root
           (list "remove" "--actor" actor "--id" task-id))
    (blackdog-clear-cache root)
    (when (derived-mode-p 'blackdog-task-view-mode 'blackdog-task-artifact-view-mode)
      (kill-buffer (current-buffer)))
    (message "Removed %s" task-id)))

(defun blackdog-task--insert-quick-links (task)
  "Insert quick artifact links for TASK."
  (let ((conversation-href (alist-get 'primary_conversation_entries_href task))
        (result-href (blackdog-task-artifact-href task 'result))
        (diff-href (blackdog-task-artifact-href task 'diff))
        (prompt-href (blackdog-task-artifact-href task 'prompt))
        (thread-href (blackdog-task-artifact-href task 'thread)))
    (when (or conversation-href result-href diff-href prompt-href thread-href)
      (insert "Artifact Links\n")
      (when conversation-href
        (insert "- ")
        (insert-text-button
         "Conversation"
         'follow-link t
         'action (lambda (_button)
                   (blackdog-task-open-conversation task)))
        (insert "\n"))
      (when prompt-href
        (insert "- ")
        (insert-text-button
         "Prompt"
         'follow-link t
         'action (lambda (_button)
                   (blackdog-task-view-browse-prompt task)))
        (insert "\n"))
      (when thread-href
        (insert "- ")
        (insert-text-button
         "Thread"
         'follow-link t
         'action (lambda (_button)
                   (blackdog-task-view-browse-thread task)))
        (insert "\n"))
      (when diff-href
        (insert "- ")
        (insert-text-button
         "Diff"
         'follow-link t
         'action (lambda (_button)
                   (blackdog-task-view-open-diff task)))
        (insert "\n"))
      (when result-href
        (insert "- ")
        (insert-text-button
         "Result"
         'follow-link t
         'action (lambda (_button)
                   (blackdog-task-view-open-result task)))
        (insert "\n"))
      (insert "\n"))))

(defun blackdog-task-open-conversation (&optional task root)
  "Open one linked conversation thread for TASK under ROOT."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (task (blackdog-task--task-for-action task))
         (threads (alist-get 'conversation_threads task))
         (thread (cond
                  ((null threads)
                   (user-error "Task %s has no linked conversation thread" (alist-get 'id task)))
                  ((= (length threads) 1)
                   (car threads))
                  (t
                   (require 'blackdog-thread)
                   (blackdog-read-thread "Conversation: " root (alist-get 'id task))))))
    (require 'blackdog-thread)
    (blackdog-thread-view thread root)))

(defun blackdog-task-view-open-result (&optional task)
  "Open the latest result artifact for TASK."
  (interactive)
  (let ((task (blackdog-task--task-for-action task)))
    (unless task
      (user-error "No task selected"))
    (blackdog-task-open-result task)))

(defun blackdog-task-view-open-prompt (&optional task)
  "Open the prompt artifact for TASK."
  (interactive)
  (let ((task (blackdog-task--task-for-action task)))
    (unless task
      (user-error "No task selected"))
    (blackdog-task-open-prompt task)))

(defun blackdog-task-view-browse-prompt (&optional task)
  "Open a read-only prompt browser for TASK."
  (interactive)
  (let ((task (blackdog-task--task-for-action task)))
    (unless task
      (user-error "No task selected"))
    (blackdog-task-browse-artifact 'prompt task blackdog-buffer-root)))

(defun blackdog-task-view-browse-thread (&optional task)
  "Open a read-only thread browser for TASK."
  (interactive)
  (let ((task (blackdog-task--task-for-action task)))
    (unless task
      (user-error "No task selected"))
    (blackdog-task-browse-artifact 'thread task blackdog-buffer-root)))

(defun blackdog-task-view-open-diff (&optional task)
  "Open the latest diff artifact for TASK."
  (interactive)
  (let ((task (blackdog-task--task-for-action task)))
    (unless task
      (user-error "No task selected"))
    (if (blackdog-task-artifact-href task 'diff)
        (blackdog-task-open-diff task)
      (require 'blackdog-magit)
      (blackdog-magit-diff-task task blackdog-buffer-root))))

(provide 'blackdog-task)

;;; blackdog-task.el ends here
