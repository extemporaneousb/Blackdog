;;; blackdog-search.el --- Search and navigation helpers for Blackdog -*- lexical-binding: t; -*-

;; Copyright (C) 2026

;;; Commentary:

;; Completion and grep helpers for Blackdog tasks, artifacts, and project files.

;;; Code:

(require 'blackdog-artifacts)
(require 'blackdog-core)
(require 'blackdog-task)
(require 'project)
(require 'seq)
(require 'subr-x)

(declare-function consult-ripgrep "consult" (&optional dir initial))

(defun blackdog-find-task (&optional root)
  "Prompt for one task from ROOT and open its reader."
  (interactive)
  (let ((root (or root blackdog-buffer-root (blackdog-project-root))))
    (blackdog-task-view
     (blackdog-read-task "Task: " nil root)
     root)))

(defun blackdog-search--display-href (href snapshot root)
  "Return a readable display string for HREF from SNAPSHOT or ROOT."
  (let* ((control-dir (blackdog-control-dir snapshot root))
         (resolved (blackdog-resolve-href href snapshot root)))
    (cond
     ((null resolved) "")
     ((string-match-p "\\`https?://" resolved) resolved)
     ((and control-dir
           (file-name-absolute-p resolved)
           (file-in-directory-p resolved control-dir))
      (file-relative-name resolved control-dir))
     ((file-name-absolute-p resolved)
      (abbreviate-file-name resolved))
     (t href))))

(defun blackdog-search--dedupe-artifact-rows (rows)
  "Return ROWS with duplicate label/href entries removed."
  (let ((seen (make-hash-table :test #'equal))
        deduped)
    (dolist (row rows (nreverse deduped))
      (let ((key (cons (alist-get 'label row)
                       (alist-get 'href row))))
        (unless (gethash key seen)
          (puthash key t seen)
          (push row deduped))))))

(defun blackdog-search--task-artifact-rows (&optional snapshot root)
  "Return artifact rows assembled from SNAPSHOT or ROOT."
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (snapshot (or snapshot (blackdog-snapshot root)))
         rows)
    (dolist (task (alist-get 'tasks snapshot))
      (let* ((task-id (alist-get 'id task))
             (status (or (alist-get 'operator_status task)
                         (alist-get 'status task)
                         "?"))
             (task-rows
              (mapcar
               (lambda (link)
                 (let ((href (alist-get 'href link)))
                   (list
                    (cons 'task task)
                    (cons 'task_id task-id)
                    (cons 'status status)
                    (cons 'artifact (alist-get 'artifact link))
                    (cons 'label (or (alist-get 'label link) "Artifact"))
                    (cons 'href href)
                    (cons 'display
                          (format "[%s] %s %s :: %s"
                                  status
                                  task-id
                                  (or (alist-get 'label link) "Artifact")
                                  (blackdog-search--display-href href snapshot root))))))
               (blackdog-task-artifacts-links task))))
        (when-let ((run-href (blackdog-task-artifact-href task 'run)))
          (push (list
                 (cons 'task task)
                 (cons 'task_id task-id)
                 (cons 'status status)
                 (cons 'artifact 'run)
                 (cons 'label "Run")
                 (cons 'href run-href)
                 (cons 'display
                       (format "[%s] %s Run :: %s"
                               status
                               task-id
                               (blackdog-search--display-href run-href snapshot root))))
                task-rows))
        (setq rows (nconc (blackdog-search--dedupe-artifact-rows task-rows) rows))))
    (sort rows
          (lambda (left right)
            (string<
             (alist-get 'display left)
             (alist-get 'display right))))))

(defun blackdog-artifact-candidates (&optional snapshot root)
  "Return completion candidates for task artifacts from SNAPSHOT or ROOT."
  (mapcar (lambda (row)
            (cons (alist-get 'display row) row))
          (blackdog-search--task-artifact-rows snapshot root)))

(defun blackdog-find-artifact (&optional root)
  "Prompt for one task artifact from ROOT and open it."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (snapshot (blackdog-snapshot root))
         (candidates (blackdog-artifact-candidates snapshot root)))
    (unless candidates
      (user-error "No task artifacts available"))
    (let* ((choice (completing-read "Artifact: " candidates nil t))
           (row (cdr (assoc choice candidates)))
           (artifact (alist-get 'artifact row))
           (task (alist-get 'task row))
           (href (alist-get 'href row)))
      (if artifact
          (blackdog-task-open-artifact artifact task root t)
        (blackdog-open-href href snapshot root t)))))

(defun blackdog-search--project-files (root)
  "Return project file paths under ROOT."
  (let ((default-directory root))
    (when-let ((project (project-current nil)))
      (project-files project))))

(defun blackdog-project-file-candidates (&optional root)
  "Return completion candidates for project files under ROOT."
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (files (or (blackdog-search--project-files root)
                    (user-error "No project found for %s" root))))
    (mapcar (lambda (path)
              (let* ((absolute (expand-file-name path root))
                     (relative (file-relative-name absolute root)))
                (cons relative relative)))
            (sort files #'string<))))

(defun blackdog-find-project-file (&optional root)
  "Prompt for one project file from ROOT and open it."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (candidates (blackdog-project-file-candidates root)))
    (unless candidates
      (user-error "No project files available"))
    (let ((choice (completing-read "Project file: " candidates nil t)))
      (blackdog-open-project-path (cdr (assoc choice candidates)) root t))))

(defun blackdog-search--grep-directory (directory &optional initial)
  "Search DIRECTORY, optionally seeding INITIAL input."
  (unless (file-directory-p directory)
    (user-error "Search root does not exist: %s" directory))
  (cond
   ((fboundp 'consult-ripgrep)
    (consult-ripgrep directory initial))
   (t
    (let ((default-directory directory))
      (call-interactively #'rgrep)))))

(defun blackdog-search-project (&optional root initial)
  "Search the project rooted at ROOT, optionally with INITIAL input."
  (interactive)
  (blackdog-search--grep-directory
   (or root blackdog-buffer-root (blackdog-project-root))
   initial))

(defun blackdog-search-artifacts (&optional root initial)
  "Search the Blackdog control dir for ROOT, optionally with INITIAL input."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (snapshot (blackdog-snapshot root))
         (control-dir (blackdog-control-dir snapshot root)))
    (blackdog-search--grep-directory control-dir initial)))

(provide 'blackdog-search)

;;; blackdog-search.el ends here
